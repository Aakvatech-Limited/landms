"""TCB inbound HTTP endpoints — IPN callback and manual reconciliation trigger.

URL paths:

  POST /api/method/landms.api.tcb.ipn_callback
       Headers: X-TCB-Token: <callback_token>
       Body:    { "reference": "...", "amount": ..., "transaction_id": "...",
                  "trans_date": "YYYY-MM-DD", "receipt_no": "..." }

  POST /api/method/landms.api.tcb.run_reconciliation
       (whitelisted; admin/system manager only)
       Body:    { "start_date": "...", "end_date": "..." }

Both endpoints honour the modes set in TCB Integration Settings:
  - inbound_mode = Off            → 401-style refusal
  - inbound_mode = Log Only       → log the IPN, do not create Payment Entry
  - inbound_mode = Apply Payment  → create the Payment Entry if auto-apply is on
"""

import json
from typing import Any

import frappe
from frappe.utils import cint, cstr, flt

from landms.tcb import (
	apply_tcb_payment_to_sales_order,
	create_tcb_api_log,
	get_tcb_inbound_mode,
	has_duplicate_ipn,
	is_callback_auto_apply_enabled,
	run_tcb_reconciliation_job,
	validate_callback_token,
)


# ---------------------------------------------------------------------- #
#  IPN callback                                                            #
# ---------------------------------------------------------------------- #


@frappe.whitelist(allow_guest=True)
def ipn_callback() -> dict[str, Any]:
	"""Handle inbound TCB IPN payment notifications.

	Authentication: callback_token in the X-TCB-Token header (or `token` form
	field as a fallback for systems that can't set custom headers).

	The endpoint always returns 200 with a JSON status — TCB will retry on
	non-200 responses, and we never want to look like an outage.
	"""
	raw_payload, payload = _read_request_payload()

	# 1. Auth — never proceed without a valid token.
	provided_token = (
		frappe.get_request_header("X-TCB-Token")
		or payload.get("token")
		or ""
	)
	if not validate_callback_token(provided_token):
		create_tcb_api_log(
			direction="Inbound",
			event_type="IPN Callback",
			status="Failed",
			processing_mode="Auth",
			endpoint="/api/method/landms.api.tcb.ipn_callback",
			external_reference=cstr(payload.get("reference") or "")[:140],
			transaction_id=cstr(payload.get("transaction_id") or "")[:140],
			request_payload=raw_payload,
			response_payload={"message": "Invalid or missing callback token."},
			error="Auth failure: token mismatch.",
		)
		return {"ok": False, "status": "Unauthorized", "message": "Invalid callback token."}

	mode = get_tcb_inbound_mode()
	reference = cstr(payload.get("reference") or "").strip()
	amount = flt(payload.get("amount"))
	transaction_id = cstr(payload.get("transaction_id") or "").strip()
	receipt_no = cstr(payload.get("receipt_no") or "").strip()
	payment_date = payload.get("trans_date") or payload.get("payment_date")
	payment_ref = receipt_no or transaction_id or reference

	endpoint = "/api/method/landms.api.tcb.ipn_callback"

	# 2. Off mode — log and decline.
	if mode == "Off":
		create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Ignored",
			processing_mode="Off", endpoint=endpoint,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Inbound mode Off."},
		)
		return {"ok": True, "status": "Ignored", "message": "Inbound mode is Off."}

	# 3. Idempotency — duplicate IPN with same transaction_id + reference.
	if transaction_id and has_duplicate_ipn(transaction_id, reference):
		create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Ignored",
			processing_mode=mode, endpoint=endpoint, is_duplicate=1,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Duplicate IPN — already processed."},
		)
		return {"ok": True, "status": "Ignored", "message": "Duplicate IPN."}

	# 4. Log Only — log and exit.
	if mode == "Log Only":
		create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Success",
			processing_mode="Log Only", endpoint=endpoint,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Log Only — payment not applied."},
		)
		return {"ok": True, "status": "Logged", "message": "IPN logged (Log Only mode)."}

	# 5. Apply Payment — only if auto-apply is on.
	if not is_callback_auto_apply_enabled():
		create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Ignored",
			processing_mode="Apply Payment", endpoint=endpoint,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Auto-apply switch is OFF — payment not created."},
		)
		return {"ok": True, "status": "Logged", "message": "IPN logged; auto-apply is off."}

	# Validate minimum payload before going to the matcher.
	if not reference or amount <= 0:
		create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Failed",
			processing_mode="Apply Payment", endpoint=endpoint,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Missing reference or non-positive amount."},
		)
		return {"ok": False, "status": "Failed", "message": "Missing reference or non-positive amount."}

	result = apply_tcb_payment_to_sales_order(
		control_number=reference,
		amount=amount,
		payment_date=payment_date,
		payment_reference=payment_ref,
	)

	create_tcb_api_log(
		direction="Inbound", event_type="IPN Callback",
		status=result.get("status") or ("Success" if result.get("ok") else "Failed"),
		processing_mode="Apply Payment", endpoint=endpoint,
		external_reference=reference, transaction_id=transaction_id,
		sales_order=result.get("sales_order"),
		payment_entry=result.get("payment_entry"),
		request_payload=raw_payload,
		response_payload={"message": result.get("message")},
		error=result.get("error"),
	)

	return {
		"ok": result.get("ok", False),
		"status": result.get("status"),
		"message": result.get("message"),
		"sales_order": result.get("sales_order"),
		"payment_entry": result.get("payment_entry"),
	}


# ---------------------------------------------------------------------- #
#  Manual reconciliation trigger                                           #
# ---------------------------------------------------------------------- #


@frappe.whitelist()
def run_reconciliation(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
	"""Manually run the TCB reconciliation pull.

	Permission: System Manager only — checked explicitly because @whitelist
	without `allow_guest` allows any logged-in user otherwise.
	"""
	if "System Manager" not in frappe.get_roles():
		frappe.throw("Only System Manager can trigger TCB reconciliation manually.")
	return run_tcb_reconciliation_job(start_date=start_date, end_date=end_date)


# ---------------------------------------------------------------------- #
#  Helpers                                                                 #
# ---------------------------------------------------------------------- #


def _read_request_payload() -> tuple[Any, dict[str, Any]]:
	"""Read request body for both JSON and form-encoded callbacks.

	Returns (raw_payload_for_logging, parsed_dict_for_processing).
	Logging gets the raw body so we can debug TCB-side malformations later.
	"""
	raw_body = ""
	parsed: dict[str, Any] = {}

	try:
		request = frappe.local.request
		raw_body = (request.get_data(as_text=True) or "").strip()
	except Exception:
		raw_body = ""

	if raw_body:
		try:
			parsed = json.loads(raw_body)
			if not isinstance(parsed, dict):
				parsed = {"data": parsed}
		except Exception:
			parsed = {}

	# Fall back to form_dict (Frappe merges JSON + form + query params here).
	if not parsed:
		parsed = dict(frappe.form_dict or {})
		# Strip Frappe internals.
		for k in ("cmd", "csrf_token"):
			parsed.pop(k, None)

	return (raw_body or parsed), parsed
