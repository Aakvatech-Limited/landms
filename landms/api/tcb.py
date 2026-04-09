"""TCB inbound HTTP endpoints — IPN callback and manual reconciliation trigger.

URL paths:

  POST /api/method/landms.api.tcb.ipn_callback
       Headers: X-TCB-Token: <callback_token>
       Body:    The TCB envelope:
                {
                  "status": 0,
                  "statusDesc": "Success",
                  "param": {
                    "transaction_id": "...",
                    "reference":      "...",
                    "amount":         350000,
                    "currency":       "TZS",
                    "transaction_date": "2020-12-29T15:05:21.000+0300",
                    "phone":          "...",
                    "description":    "...",
                    "account_no":     "...",
                    "charge":         0.0
                  }
                }
                Flat (non-envelope) bodies are still accepted for dev testing.

  POST /api/method/landms.api.tcb.run_reconciliation
       (whitelisted; admin/system manager only)

Both endpoints honour the modes set in TCB Integration Settings:
  - inbound_mode = Off            → refusal (logged)
  - inbound_mode = Log Only       → log + persist notification, do not create Payment Entry
  - inbound_mode = Apply Payment  → create Payment Entry if auto-apply is on
"""

import json
from typing import Any

import frappe
from frappe.utils import cint, cstr, flt, now

from landms.tcb import (
	apply_tcb_payment_to_sales_order,
	create_tcb_api_log,
	get_tcb_inbound_mode,
	has_duplicate_ipn,
	is_callback_auto_apply_enabled,
	run_tcb_reconciliation_job,
	validate_callback_token,
)


IPN_ENDPOINT = "/api/method/landms.api.tcb.ipn_callback"


# ---------------------------------------------------------------------- #
#  IPN callback                                                            #
# ---------------------------------------------------------------------- #


@frappe.whitelist(allow_guest=True)
def ipn_callback() -> dict[str, Any]:
	"""Handle inbound TCB IPN payment notifications.

	Authentication: callback_token in the X-TCB-Token header (or `token` form
	field as a fallback for systems that can't set custom headers).

	Always returns 200 with a JSON body — TCB retries on non-200 and we never
	want to look like an outage. The full raw payload is persisted to
	`TCB Payment Notification` for audit and manual replay.
	"""
	raw_payload, envelope = _read_request_payload()
	# Unwrap the envelope: the real TCB body wraps payment details inside `param`.
	body = _extract_ipn_body(envelope)

	# 1. Auth — never proceed without a valid token.
	provided_token = (
		frappe.get_request_header("X-TCB-Token")
		or envelope.get("token")
		or body.get("token")
		or ""
	)
	if not validate_callback_token(provided_token):
		create_tcb_api_log(
			direction="Inbound",
			event_type="IPN Callback",
			status="Failed",
			processing_mode="Auth",
			endpoint=IPN_ENDPOINT,
			external_reference=cstr(body.get("reference") or "")[:140],
			transaction_id=cstr(body.get("transaction_id") or "")[:140],
			request_payload=raw_payload,
			response_payload={"message": "Invalid or missing callback token."},
			error="Auth failure: token mismatch.",
		)
		return {"ok": False, "status": "Unauthorized", "message": "Invalid callback token."}

	mode = get_tcb_inbound_mode()
	reference = cstr(body.get("reference") or "").strip()
	amount = flt(body.get("amount"))
	transaction_id = cstr(body.get("transaction_id") or "").strip()
	payment_date = body.get("transaction_date") or body.get("trans_date") or body.get("payment_date")
	payment_ref = transaction_id or reference

	# 2. Persist the notification record FIRST — even if we'll ignore it.
	#    This is the canonical audit trail of what TCB sent us.
	notification_name = _upsert_payment_notification(
		envelope=envelope,
		body=body,
		raw_payload=raw_payload,
		initial_status="Received",
	)

	def _finalize(notification_status: str, *, message: str = "",
	              error: str = "", sales_order: str = "",
	              payment_entry: str = "", log_name: str = "") -> None:
		if not notification_name:
			return
		try:
			frappe.db.set_value("TCB Payment Notification", notification_name, {
				"status":             notification_status,
				"processing_message": (message or "")[:500],
				"error":              error or "",
				"sales_order":        sales_order or "",
				"payment_entry":      payment_entry or "",
				"tcb_api_log":        log_name or "",
			}, update_modified=True)
		except Exception:
			frappe.logger("landms").error(
				"Failed to update TCB Payment Notification",
				exc_info=True,
			)

	# 3. Off mode — log and decline.
	if mode == "Off":
		log_name = create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Ignored",
			processing_mode="Off", endpoint=IPN_ENDPOINT,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Inbound mode Off."},
		)
		_finalize("Ignored", message="Inbound mode is Off.", log_name=log_name or "")
		return {"ok": True, "status": "Ignored", "message": "Inbound mode is Off."}

	# 4. Idempotency — duplicate IPN with same transaction_id + reference.
	if transaction_id and has_duplicate_ipn(transaction_id, reference):
		log_name = create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Ignored",
			processing_mode=mode, endpoint=IPN_ENDPOINT, is_duplicate=1,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Duplicate IPN — already processed."},
		)
		_finalize("Duplicate", message="Duplicate IPN — already processed.", log_name=log_name or "")
		return {"ok": True, "status": "Ignored", "message": "Duplicate IPN."}

	# 5. Log Only — log and exit.
	if mode == "Log Only":
		log_name = create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Success",
			processing_mode="Log Only", endpoint=IPN_ENDPOINT,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Log Only — payment not applied."},
		)
		_finalize("Ignored", message="Log Only mode — payment not applied.", log_name=log_name or "")
		return {"ok": True, "status": "Logged", "message": "IPN logged (Log Only mode)."}

	# 6. Apply Payment — only if auto-apply is on.
	if not is_callback_auto_apply_enabled():
		log_name = create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Ignored",
			processing_mode="Apply Payment", endpoint=IPN_ENDPOINT,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Auto-apply switch is OFF — payment not created."},
		)
		_finalize("Ignored", message="Auto-apply is off — payment not created.", log_name=log_name or "")
		return {"ok": True, "status": "Logged", "message": "IPN logged; auto-apply is off."}

	# 7. Validate minimum payload before going to the matcher.
	if not reference or amount <= 0:
		log_name = create_tcb_api_log(
			direction="Inbound", event_type="IPN Callback", status="Failed",
			processing_mode="Apply Payment", endpoint=IPN_ENDPOINT,
			external_reference=reference, transaction_id=transaction_id,
			request_payload=raw_payload,
			response_payload={"message": "Missing reference or non-positive amount."},
		)
		_finalize("Failed", message="Missing reference or non-positive amount.",
		          error="Validation failure.", log_name=log_name or "")
		return {"ok": False, "status": "Failed", "message": "Missing reference or non-positive amount."}

	result = apply_tcb_payment_to_sales_order(
		control_number=reference,
		amount=amount,
		payment_date=payment_date,
		payment_reference=payment_ref,
	)

	log_name = create_tcb_api_log(
		direction="Inbound", event_type="IPN Callback",
		status=result.get("status") or ("Success" if result.get("ok") else "Failed"),
		processing_mode="Apply Payment", endpoint=IPN_ENDPOINT,
		external_reference=reference, transaction_id=transaction_id,
		sales_order=result.get("sales_order"),
		payment_entry=result.get("payment_entry"),
		request_payload=raw_payload,
		response_payload={"message": result.get("message")},
		error=result.get("error"),
	)

	final_status = {
		"Success": "Processed",
		"Ignored": "Duplicate",
		"Failed":  "Failed",
	}.get(result.get("status") or "", "Failed")

	_finalize(
		final_status,
		message=result.get("message") or "",
		error=result.get("error") or "",
		sales_order=result.get("sales_order") or "",
		payment_entry=result.get("payment_entry") or "",
		log_name=log_name or "",
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
	Logging gets the raw body AND request headers so we can see exactly what
	TCB sent — including any auth header they may use that isn't documented.
	"""
	raw_body = ""
	parsed: dict[str, Any] = {}
	headers: dict[str, str] = {}
	method = ""
	source_ip = ""
	query_string = ""

	try:
		request = frappe.local.request
		raw_body = (request.get_data(as_text=True) or "").strip()
		headers = {k: v for k, v in request.headers.items()}
		method = request.method or ""
		source_ip = request.remote_addr or ""
		query_string = request.query_string.decode("utf-8", errors="replace") if request.query_string else ""
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

	# Bundle headers + body together so the persisted raw_payload shows
	# the full incoming request. This is the only place TCB's actual
	# request shape is captured for auditing.
	debug_envelope = {
		"method":       method,
		"source_ip":    source_ip,
		"query_string": query_string,
		"headers":      headers,
		"body":         raw_body or parsed,
	}

	return debug_envelope, parsed


def _extract_ipn_body(envelope: dict[str, Any]) -> dict[str, Any]:
	"""Return the inner payment-detail dict from a TCB IPN envelope.

	TCB wraps details inside a `param` key:
	    {"status":0,"statusDesc":"Success","param":{...payment...}}
	If `param` is absent or not a dict, treat the envelope itself as the body
	(covers dev-time flat payloads and accidental shape changes).
	"""
	if not isinstance(envelope, dict):
		return {}
	param = envelope.get("param")
	if isinstance(param, dict) and param:
		return param
	# The envelope itself IS the body (flat payload).
	return envelope


def _upsert_payment_notification(
	*,
	envelope: dict[str, Any],
	body: dict[str, Any],
	raw_payload: Any,
	initial_status: str,
) -> str | None:
	"""Insert a TCB Payment Notification row for this IPN, keyed on transaction_id.

	Returns the docname, or None if persistence failed (logged but never raised —
	the callback must keep functioning even if the audit table is broken).

	If a row with the same transaction_id already exists, we reuse it and
	return the existing name — the IPN handler will then mark it as Duplicate
	via the idempotency guard.
	"""
	transaction_id = cstr(body.get("transaction_id") or "").strip()
	try:
		if transaction_id:
			existing = frappe.db.get_value(
				"TCB Payment Notification",
				{"transaction_id": transaction_id},
				"name",
			)
			if existing:
				return existing

		doc = frappe.get_doc({
			"doctype":          "TCB Payment Notification",
			"status":           initial_status,
			"received_at":      now(),
			"tcb_status_code":  _safe_int(envelope.get("status")),
			"tcb_status_desc":  cstr(envelope.get("statusDesc") or envelope.get("status_desc") or "")[:140],
			"transaction_id":   transaction_id,
			"reference":        cstr(body.get("reference") or "")[:140],
			"account_no":       cstr(body.get("account_no") or "")[:140],
			"amount":           flt(body.get("amount")),
			"currency":         cstr(body.get("currency") or "TZS")[:20],
			"charge":           flt(body.get("charge")),
			"transaction_date": _parse_tcb_datetime(body.get("transaction_date") or body.get("trans_date")),
			"phone":            cstr(body.get("phone") or "")[:140],
			"description":      cstr(body.get("description") or "")[:1000],
			"tcb_control_number": (
				body.get("reference")
				if body.get("reference")
				and frappe.db.exists("TCB Control Number", body.get("reference"))
				else ""
			),
			"raw_payload":      _to_text(raw_payload),
		})
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.logger("landms").error(
			"Failed to insert TCB Payment Notification",
			exc_info=True,
		)
		return None


def _safe_int(value) -> int | None:
	try:
		return cint(value) if value is not None else None
	except Exception:
		return None


def _parse_tcb_datetime(value) -> str | None:
	"""TCB sends `2020-12-29T15:05:21.000+0300`. Convert to a Frappe-friendly string.

	Falls back to None on parse failure — the field accepts empty values.
	"""
	if not value:
		return None
	try:
		from frappe.utils import get_datetime
		return str(get_datetime(value))
	except Exception:
		return cstr(value)[:30] or None


def _to_text(data) -> str:
	if data is None:
		return ""
	if isinstance(data, str):
		return data
	try:
		return json.dumps(data, default=str, indent=2, sort_keys=True)
	except Exception:
		return str(data)
