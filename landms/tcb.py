"""TCB integration module — control number lifecycle, outbound and inbound.

Public surface (used by sales_order_hooks, api/tcb.py, scheduler jobs):

  generate_control_number(sales_order_name)              → str
  is_valid_control_number(value, pattern=None)           → bool
  register_reference_for_sales_order(so_name, cn)        → dict
  decline_reference_for_sales_order(so_name, cn)         → dict
  apply_tcb_payment_to_sales_order(...)                  → dict
  run_tcb_reconciliation_job(start_date, end_date)       → dict
  validate_callback_token(token)                         → bool
  get_tcb_inbound_mode()                                 → str
  is_callback_auto_apply_enabled()                       → bool
  has_duplicate_ipn(transaction_id, reference)           → bool
  create_tcb_api_log(...)                                → str | None

Design notes:
  - Outbound payload to TCB Reference Create contains ONLY partnerCode +
    profileID + reference. No customer name, no mobile, no message body.
    TCB just confirms / rejects the reference.
  - Control number generation uses secrets.randbelow (NEVER random).
  - Every call writes a TCB API Log row, then appends to the
    TCB Control Number registry trail when a control number is in scope.
  - Settings are read defensively — missing TCB Integration Settings is
    treated the same as enabled=0 / mode=Off.
"""

import json
import re
import secrets
from hmac import compare_digest
from typing import Any

import frappe
from frappe.utils import (
	add_days,
	cint,
	cstr,
	flt,
	get_datetime,
	get_request_session,
	now,
	today,
)


DEFAULT_PATTERN = "99911####00##"
GENERATION_RETRIES = 20


# ---------------------------------------------------------------------- #
#  Pattern handling                                                        #
# ---------------------------------------------------------------------- #


def _get_pattern() -> str:
	"""Read the control number pattern from TCB Integration Settings.

	Falls back to DEFAULT_PATTERN if the setting doc doesn't exist or the
	field is empty. We never raise here — generation should be possible
	even before the user has touched the settings.
	"""
	try:
		pattern = frappe.db.get_single_value("TCB Integration Settings", "control_number_pattern")
	except Exception:
		pattern = None
	return (pattern or DEFAULT_PATTERN).strip()


def _pattern_to_regex(pattern: str) -> re.Pattern:
	"""Compile a control number pattern into an anchored regex.

	'#' becomes \\d, every other character is matched literally (escaped).
	"""
	parts = []
	for ch in pattern:
		if ch == "#":
			parts.append(r"\d")
		else:
			parts.append(re.escape(ch))
	return re.compile("^" + "".join(parts) + "$")


def is_valid_control_number(value: str, pattern: str | None = None) -> bool:
	"""Strict pattern check — same shape used by generation."""
	value = cstr(value).strip()
	if not value:
		return False
	pattern = (pattern or _get_pattern()).strip()
	if not pattern:
		return False
	return bool(_pattern_to_regex(pattern).match(value))


# ---------------------------------------------------------------------- #
#  Generation                                                              #
# ---------------------------------------------------------------------- #


def _fill_pattern(pattern: str) -> str:
	"""Substitute every '#' with a cryptographically secure random digit."""
	out = []
	for ch in pattern:
		if ch == "#":
			out.append(str(secrets.randbelow(10)))
		else:
			out.append(ch)
	return "".join(out)


def _control_number_exists(candidate: str) -> bool:
	"""True if the candidate is already in use anywhere we care about.

	Two sources of truth:
	  - tabSales Order.control_number (the field that drives lookups)
	  - tabTCB Control Number          (the registry — DB-level unique by name)

	Both are checked. Wrapped in try/except so a missing column on a fresh
	install can't break generation.
	"""
	try:
		if frappe.db.has_column("Sales Order", "control_number"):
			if frappe.db.exists("Sales Order", {"control_number": candidate}):
				return True
	except Exception:
		pass
	try:
		if frappe.db.exists("TCB Control Number", candidate):
			return True
	except Exception:
		pass
	return False


def generate_control_number(sales_order_name: str | None = None) -> str:
	"""Generate a unique TCB control number.

	The pattern is read from TCB Integration Settings. The result is checked
	against both the Sales Order column and the TCB Control Number registry
	to avoid collisions. Retries up to GENERATION_RETRIES times before throwing.

	`sales_order_name` is accepted for symmetry with the legacy signature; it
	does NOT influence the generated value (which is fully random).
	"""
	pattern = _get_pattern()
	if "#" not in pattern:
		frappe.throw(
			"Control Number Pattern in TCB Integration Settings is missing '#'. "
			"Cannot generate randomized references."
		)

	for _ in range(GENERATION_RETRIES):
		candidate = _fill_pattern(pattern)
		if not _control_number_exists(candidate):
			return candidate

	frappe.throw(
		"Failed to generate a unique TCB control number after multiple attempts. "
		"This usually means the pattern has too few random positions for the volume "
		"of references. Increase the number of '#' characters in TCB Integration Settings."
	)


# ---------------------------------------------------------------------- #
#  Settings access                                                         #
# ---------------------------------------------------------------------- #


def _get_tcb_settings() -> dict[str, Any]:
	"""Read TCB Integration Settings into a plain dict.

	If the doc doesn't exist (fresh install), return safe-off defaults so
	hooks degrade gracefully — no calls go out, no inbound is processed.
	"""
	settings_doc = _get_tcb_integration_doc()
	if not settings_doc:
		return {
			"enabled": 0,
			"outbound_mode": "Off",
			"inbound_mode": "Off",
			"control_number_pattern": DEFAULT_PATTERN,
			"auto_apply_callback_payments": 0,
			"auto_apply_reconciliation_payments": 0,
			"reconciliation_enabled": 0,
			"reconciliation_lookback_days": 1,
			"decline_reference_on_so_cancel": 1,
			"decline_failure_policy": "Allow Cancel and Flag",
			"live_base_url": "https://partners.tcbbank.co.tz",
			"reconciliation_base_url": "https://partners.tcbbank.co.tz:8444",
			"partner_code": "",
			"profile_id": "",
			"verify_ssl": 1,
			"connect_timeout_seconds": 5,
			"read_timeout_seconds": 15,
		}

	return {
		"enabled": settings_doc.enabled,
		"outbound_mode": settings_doc.outbound_mode or "Off",
		"inbound_mode": settings_doc.inbound_mode or "Off",
		"control_number_pattern": settings_doc.control_number_pattern or DEFAULT_PATTERN,
		"auto_apply_callback_payments": settings_doc.auto_apply_callback_payments,
		"auto_apply_reconciliation_payments": settings_doc.auto_apply_reconciliation_payments,
		"reconciliation_enabled": settings_doc.reconciliation_enabled,
		"reconciliation_lookback_days": settings_doc.reconciliation_lookback_days or 1,
		"decline_reference_on_so_cancel": settings_doc.decline_reference_on_so_cancel,
		"decline_failure_policy": settings_doc.decline_failure_policy or "Allow Cancel and Flag",
		"live_base_url": settings_doc.live_base_url or "https://partners.tcbbank.co.tz",
		"reconciliation_base_url": settings_doc.reconciliation_base_url or "https://partners.tcbbank.co.tz:8444",
		"api_key": settings_doc.get_password("api_key", raise_exception=False),
		"partner_code": settings_doc.partner_code,
		"profile_id": settings_doc.profile_id,
		"verify_ssl": settings_doc.verify_ssl,
		"connect_timeout_seconds": settings_doc.connect_timeout_seconds or 5,
		"read_timeout_seconds": settings_doc.read_timeout_seconds or 15,
	}


def _get_tcb_integration_doc():
	try:
		return frappe.get_single("TCB Integration Settings")
	except Exception:
		return None


def get_tcb_inbound_mode() -> str:
	return _get_tcb_settings().get("inbound_mode") or "Off"


def is_callback_auto_apply_enabled() -> bool:
	settings = _get_tcb_settings()
	return bool(
		cint(settings.get("enabled"))
		and settings.get("inbound_mode") == "Apply Payment"
		and cint(settings.get("auto_apply_callback_payments"))
	)


def is_reconciliation_enabled() -> bool:
	settings = _get_tcb_settings()
	return bool(cint(settings.get("enabled")) and cint(settings.get("reconciliation_enabled")))


def should_auto_apply_reconciliation_payments() -> bool:
	settings = _get_tcb_settings()
	return bool(
		cint(settings.get("enabled"))
		and cint(settings.get("reconciliation_enabled"))
		and cint(settings.get("auto_apply_reconciliation_payments"))
	)


def validate_callback_token(provided_token: str | None) -> bool:
	"""Constant-time comparison against the configured callback token."""
	try:
		settings_doc = frappe.get_single("TCB Integration Settings")
		expected_token = settings_doc.get_password("callback_token", raise_exception=False) or ""
	except Exception:
		return False
	if not expected_token:
		return False
	return compare_digest((provided_token or "").strip(), expected_token.strip())


# ---------------------------------------------------------------------- #
#  Registry helpers                                                        #
# ---------------------------------------------------------------------- #


def create_or_get_registry(*, control_number: str, sales_order: str,
                           customer: str | None = None, amount: float = 0):
	"""Idempotent registry creation. Returns the registry doc."""
	if frappe.db.exists("TCB Control Number", control_number):
		return frappe.get_doc("TCB Control Number", control_number)
	doc = frappe.get_doc({
		"doctype":        "TCB Control Number",
		"control_number": control_number,
		"sales_order":    sales_order,
		"customer":       customer or "",
		"amount":         flt(amount),
		"status":         "Generated",
		"generated_at":   now(),
		"last_event":     f"Generated for {sales_order}",
	})
	doc.insert(ignore_permissions=True)
	return doc


def _get_registry(control_number: str):
	if not control_number:
		return None
	if not frappe.db.exists("TCB Control Number", control_number):
		return None
	return frappe.get_doc("TCB Control Number", control_number)


# ---------------------------------------------------------------------- #
#  Outbound — Reference Create                                             #
# ---------------------------------------------------------------------- #


def _build_reference_payload(*, control_number: str) -> dict[str, Any]:
	"""Minimal outbound payload — only control number + partner auth.

	NOTE vs legacy LMS: customer name, mobile, and message body are
	intentionally omitted. TCB only validates and stores the reference
	against the partner account.
	"""
	settings = _get_tcb_settings()
	return {
		"partnerCode": settings.get("partner_code") or "",
		"profileID":   settings.get("profile_id") or "",
		"reference":   control_number,
	}


def register_reference_for_sales_order(sales_order_name: str, control_number: str) -> dict[str, Any]:
	"""Register a control number with TCB. Mode-aware (Off / Log Only / Live)."""
	settings = _get_tcb_settings()
	outbound_mode = settings.get("outbound_mode") or "Off"
	enabled = cint(settings.get("enabled"))

	payload = _build_reference_payload(control_number=control_number)
	endpoint = _masked_reference_endpoint(settings)

	if not enabled or outbound_mode == "Off":
		log_name = create_tcb_api_log(
			direction="Outbound",
			event_type="Reference Create",
			status="Ignored",
			processing_mode="Off",
			endpoint=endpoint,
			external_reference=control_number,
			sales_order=sales_order_name,
			request_payload=payload,
			response_payload={"message": "Outbound reference registration skipped (integration disabled or mode Off)."},
		)
		_record_registry_event(control_number, "Reference Create", "Ignored", log_name,
		                       note="Outbound mode Off — skipped.")
		return {"ok": True, "mode": "Off",
		        "message": "TCB outbound mode is Off; reference registration skipped."}

	if outbound_mode == "Log Only":
		log_name = create_tcb_api_log(
			direction="Outbound",
			event_type="Reference Create",
			status="Ignored",
			processing_mode="Log Only",
			endpoint=endpoint,
			external_reference=control_number,
			sales_order=sales_order_name,
			request_payload=payload,
			response_payload={"message": "Outbound Log Only mode — no call sent to TCB."},
		)
		_record_registry_event(control_number, "Reference Create", "Ignored", log_name,
		                       note="Outbound mode Log Only — no live call.")
		return {"ok": True, "mode": "Log Only",
		        "message": "TCB outbound mode is Log Only; no live call was made."}

	# --- Live ---
	_validate_live_reference_settings(settings)
	url = _reference_create_url(settings)
	verify_ssl = bool(cint(settings.get("verify_ssl", 1)))
	connect_timeout = flt(settings.get("connect_timeout_seconds") or 5)
	read_timeout = flt(settings.get("read_timeout_seconds") or 15)

	http_status = None
	parsed_body = None
	tcb_status = None
	tcb_message = None

	try:
		session = get_request_session()
		response = session.post(
			url,
			data=payload,
			timeout=(connect_timeout, read_timeout),
			verify=verify_ssl,
		)
		http_status = response.status_code
		parsed_body = _parse_json_or_text(response)
		tcb_status, tcb_message = _extract_tcb_status_message(parsed_body)
		ok = response.ok and tcb_status == 0

		log_name = create_tcb_api_log(
			direction="Outbound",
			event_type="Reference Create",
			status="Success" if ok else "Failed",
			processing_mode="Live",
			endpoint=endpoint,
			http_status_code=http_status,
			tcb_status_code=tcb_status,
			tcb_message=tcb_message,
			external_reference=control_number,
			sales_order=sales_order_name,
			request_payload=payload,
			response_payload=parsed_body,
			error=None if ok else "TCB reference API returned non-success status.",
		)

		registry = _get_registry(control_number)
		if registry:
			if ok:
				registry.mark_registered(log_name=log_name,
				                         note=tcb_message or "Registered with TCB.")
			else:
				registry.mark_failed(log_name=log_name,
				                     event_type="Reference Create",
				                     note=f"HTTP {http_status} TCB {tcb_status}: {tcb_message}")

		if not ok:
			return {
				"ok": False, "mode": "Live",
				"message": (
					f"TCB reference registration failed "
					f"(HTTP {http_status}, TCB Status {tcb_status}: {tcb_message or 'No message'})."
				),
			}

		return {
			"ok": True, "mode": "Live",
			"message": tcb_message or "TCB reference registered successfully.",
			"http_status": http_status, "tcb_status": tcb_status,
		}
	except Exception:
		traceback = frappe.get_traceback()
		log_name = create_tcb_api_log(
			direction="Outbound",
			event_type="Reference Create",
			status="Failed",
			processing_mode="Live",
			endpoint=endpoint,
			http_status_code=http_status,
			tcb_status_code=tcb_status,
			tcb_message=tcb_message,
			external_reference=control_number,
			sales_order=sales_order_name,
			request_payload=payload,
			response_payload=parsed_body,
			error=traceback,
		)
		registry = _get_registry(control_number)
		if registry:
			registry.mark_failed(log_name=log_name,
			                     event_type="Reference Create",
			                     note="Reference Create raised an exception. See log.")
		return {
			"ok": False, "mode": "Live",
			"message": "TCB reference registration raised an exception. Check TCB API Log for details.",
		}


# ---------------------------------------------------------------------- #
#  Outbound — Reference Decline                                            #
# ---------------------------------------------------------------------- #


def decline_reference_for_sales_order(sales_order_name: str, control_number: str) -> dict[str, Any]:
	"""Decline a registered TCB reference when an unpaid SO is being cancelled."""
	settings = _get_tcb_settings()
	policy = settings.get("decline_failure_policy") or "Allow Cancel and Flag"

	if not control_number:
		return {"ok": True, "status": "Ignored", "message": "No control number to decline."}
	if not cint(settings.get("enabled")):
		return {"ok": True, "status": "Ignored", "message": "TCB integration disabled."}
	if not cint(settings.get("decline_reference_on_so_cancel")):
		return {"ok": True, "status": "Ignored", "message": "Decline-on-cancel switch is OFF."}
	if settings.get("outbound_mode") != "Live":
		# Still mark the registry as Declined locally so reporting reflects it.
		registry = _get_registry(control_number)
		if registry and registry.status not in ("Paid", "Declined", "Expired"):
			registry.mark_declined(note="Outbound mode not Live — declined locally only.")
		return {"ok": True, "status": "Ignored", "message": "Outbound mode is not Live; decline call skipped."}

	_validate_live_reference_settings(settings)

	endpoint = _masked_reference_decline_endpoint(settings)
	url = _reference_decline_url(settings)
	payload = {
		"partnerCode": settings.get("partner_code") or "",
		"acctNo":      settings.get("profile_id") or "",
		"refNo":       control_number,
	}
	verify_ssl = bool(cint(settings.get("verify_ssl", 1)))
	connect_timeout = flt(settings.get("connect_timeout_seconds") or 5)
	read_timeout = flt(settings.get("read_timeout_seconds") or 15)

	http_status = None
	parsed_body = None
	tcb_status = None
	tcb_message = None
	try:
		session = get_request_session()
		response = session.post(
			url, json=payload,
			timeout=(connect_timeout, read_timeout),
			verify=verify_ssl,
		)
		http_status = response.status_code
		parsed_body = _parse_json_or_text(response)
		tcb_status, tcb_message = _extract_tcb_status_message(parsed_body)
		ok = response.ok and tcb_status == 0

		log_name = create_tcb_api_log(
			direction="Outbound",
			event_type="Reference Decline",
			status="Success" if ok else "Failed",
			processing_mode="Live",
			endpoint=endpoint,
			http_status_code=http_status,
			tcb_status_code=tcb_status,
			tcb_message=tcb_message,
			external_reference=control_number,
			sales_order=sales_order_name,
			request_payload=payload,
			response_payload=parsed_body,
			error=None if ok else "TCB decline API returned non-success status.",
		)

		registry = _get_registry(control_number)
		if registry:
			if ok:
				registry.mark_declined(log_name=log_name,
				                       note=tcb_message or "Declined at TCB.")
			else:
				registry.append_log(log_name, "Reference Decline", "Failed",
				                    note=f"HTTP {http_status} TCB {tcb_status}: {tcb_message}")

		if ok:
			return {"ok": True, "status": "Success", "message": tcb_message or "TCB reference declined."}

		block_cancel = policy == "Block Cancel"
		return {
			"ok": False, "status": "Failed", "block_cancel": block_cancel,
			"message": (
				f"TCB decline failed (HTTP {http_status}, TCB Status {tcb_status}: "
				f"{tcb_message or 'No message'})."
			),
		}
	except Exception:
		err = frappe.get_traceback()
		log_name = create_tcb_api_log(
			direction="Outbound",
			event_type="Reference Decline",
			status="Failed",
			processing_mode="Live",
			endpoint=endpoint,
			http_status_code=http_status,
			tcb_status_code=tcb_status,
			tcb_message=tcb_message,
			external_reference=control_number,
			sales_order=sales_order_name,
			request_payload=payload,
			response_payload=parsed_body,
			error=err,
		)
		registry = _get_registry(control_number)
		if registry:
			registry.append_log(log_name, "Reference Decline", "Failed",
			                    note="Reference Decline raised an exception.")
		block_cancel = policy == "Block Cancel"
		return {
			"ok": False, "status": "Failed", "block_cancel": block_cancel,
			"message": "TCB decline call failed with exception. Check TCB API Log.",
		}


# ---------------------------------------------------------------------- #
#  Inbound — Apply payment to a Sales Order                                #
# ---------------------------------------------------------------------- #


def apply_tcb_payment_to_sales_order(
	*,
	control_number: str,
	amount: float,
	payment_date: str | None,
	payment_reference: str | None,
) -> dict[str, Any]:
	"""Apply a confirmed TCB payment to the matching Sales Order's plot SI.

	Looks up the SO by control_number, then walks to its plot_sales_invoice
	(populated by Plot Contract on first advance) and creates a Payment
	Entry against it. The registry is updated to Paid on success.

	Idempotent: if a Payment Entry with the same reference_no already
	exists for the invoice, returns {status: 'Ignored'}.
	"""
	from landms.payment_sync import create_payment_entry_for_sales_order

	control_number = cstr(control_number).strip()
	payment_reference = cstr(payment_reference).strip()
	amount = flt(amount)
	payment_date = _normalize_date_string(payment_date)

	if not control_number:
		return {"ok": False, "status": "Failed", "message": "Missing control/reference number from TCB payload."}
	if amount <= 0:
		return {"ok": False, "status": "Failed", "message": "TCB payment amount must be greater than zero."}

	standard_so_name = ""
	try:
		if frappe.db.has_column("Sales Order", "control_number"):
			standard_so_name = frappe.db.get_value(
				"Sales Order",
				{"control_number": control_number, "docstatus": 1},
				"name",
			)
	except Exception:
		standard_so_name = ""

	if not standard_so_name:
		return {
			"ok": False, "status": "Failed",
			"message": f"No submitted Sales Order was found for control number {control_number}.",
		}

	reference_no = payment_reference or control_number
	if _has_payment_reference_for_sales_order(standard_so_name, reference_no):
		return {
			"ok": True, "status": "Ignored",
			"message": f"Payment reference {reference_no} already exists for Sales Order {standard_so_name}.",
			"sales_order": standard_so_name,
		}

	bank_account = frappe.db.get_single_value("LandMS Settings", "tcb_bank_account")
	if not bank_account:
		return {
			"ok": False, "status": "Failed",
			"message": "LandMS Settings is missing TCB Bank Account; cannot auto-apply payment.",
			"sales_order": standard_so_name,
		}

	try:
		pe_name = create_payment_entry_for_sales_order(
			sales_order_name=standard_so_name,
			amount=amount,
			payment_date=payment_date,
			bank_account=bank_account,
			reference_no=reference_no,
			remarks=f"TCB Payment — {standard_so_name} / Control No: {control_number}",
		)
		registry = _get_registry(control_number)
		if registry:
			registry.mark_paid(
				payment_entry=pe_name,
				payment_reference=reference_no,
				paid_amount=amount,
				note=f"Auto-applied to {standard_so_name}.",
			)
		return {
			"ok": True, "status": "Success",
			"message": f"Payment auto-applied to {standard_so_name}.",
			"sales_order": standard_so_name, "payment_entry": pe_name,
		}
	except Exception as exc:
		msg = cstr(exc)
		if "Duplicate payment reference" in msg:
			return {
				"ok": True, "status": "Ignored", "message": msg,
				"sales_order": standard_so_name,
			}
		return {
			"ok": False, "status": "Failed",
			"message": msg or "Failed to auto-apply TCB payment.",
			"sales_order": standard_so_name, "error": frappe.get_traceback(),
		}


# ---------------------------------------------------------------------- #
#  Reconciliation pull                                                     #
# ---------------------------------------------------------------------- #


def run_tcb_reconciliation_job(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
	"""Pull reconciliation rows from TCB and optionally auto-apply missing payments.

	Wired to scheduler in Phase 8. The dry path (auto_apply off) just logs.
	"""
	settings = _get_tcb_settings()
	if not cint(settings.get("enabled")):
		return {"ok": True, "status": "Ignored", "message": "TCB integration disabled."}
	if not cint(settings.get("reconciliation_enabled")):
		return {"ok": True, "status": "Ignored", "message": "TCB reconciliation is disabled in settings."}

	lookback_days = cint(settings.get("reconciliation_lookback_days") or 1)
	end = _normalize_date_string(end_date)
	start = _normalize_date_string(start_date) if start_date else _normalize_date_string(add_days(end, -lookback_days))

	fetch_result = _fetch_reconciliation_rows(settings=settings, start_date=start, end_date=end)
	rows = fetch_result.get("rows") or []
	if not fetch_result.get("ok"):
		return {
			"ok": False, "status": "Failed",
			"message": fetch_result.get("message") or "TCB reconciliation fetch failed.",
		}

	applied = 0
	ignored = 0
	failed = 0
	for row in rows:
		reference = cstr(row.get("reference") or "").strip()
		amount = flt(row.get("amount"))
		transaction_id = cstr(row.get("ptid") or "").strip()
		receipt_no = cstr(row.get("receipt_no") or "").strip()
		payment_ref = receipt_no or transaction_id or reference
		payment_date = row.get("trans_date")

		if not reference or amount <= 0:
			create_tcb_api_log(
				direction="Inbound",
				event_type="Reconciliation",
				status="Failed",
				processing_mode="Log Only",
				endpoint=_masked_reconciliation_endpoint(settings),
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=row,
				response_payload={"message": "Skipped row due to missing reference or non-positive amount."},
			)
			failed += 1
			continue

		if not cint(settings.get("auto_apply_reconciliation_payments")):
			create_tcb_api_log(
				direction="Inbound",
				event_type="Reconciliation",
				status="Ignored",
				processing_mode="Log Only",
				endpoint=_masked_reconciliation_endpoint(settings),
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=row,
				response_payload={"message": "Auto-apply reconciliation switch is OFF."},
			)
			ignored += 1
			continue

		result = apply_tcb_payment_to_sales_order(
			control_number=reference,
			amount=amount,
			payment_date=payment_date,
			payment_reference=payment_ref,
		)
		create_tcb_api_log(
			direction="Inbound",
			event_type="Reconciliation",
			status=result.get("status") or ("Success" if result.get("ok") else "Failed"),
			processing_mode="Apply Payment",
			endpoint=_masked_reconciliation_endpoint(settings),
			external_reference=reference,
			transaction_id=transaction_id,
			sales_order=result.get("sales_order"),
			payment_entry=result.get("payment_entry"),
			request_payload=row,
			response_payload={"message": result.get("message")},
			error=result.get("error"),
		)

		status = result.get("status")
		if status == "Success":
			applied += 1
		elif status == "Ignored":
			ignored += 1
		else:
			failed += 1

	return {
		"ok": True, "status": "Success",
		"message": f"TCB reconciliation processed {len(rows)} rows: applied={applied}, ignored={ignored}, failed={failed}.",
		"rows": len(rows), "applied": applied, "ignored": ignored, "failed": failed,
		"start_date": start, "end_date": end,
	}


# ---------------------------------------------------------------------- #
#  Logging                                                                 #
# ---------------------------------------------------------------------- #


def create_tcb_api_log(
	*,
	direction: str,
	event_type: str,
	status: str,
	processing_mode: str | None = None,
	endpoint: str | None = None,
	http_status_code: int | None = None,
	tcb_status_code: int | None = None,
	tcb_message: str | None = None,
	external_reference: str | None = None,
	transaction_id: str | None = None,
	sales_order: str | None = None,
	plot_contract: str | None = None,
	payment_entry: str | None = None,
	request_payload: Any = None,
	response_payload: Any = None,
	error: str | None = None,
	is_duplicate: int = 0,
) -> str | None:
	"""Best-effort insertion into TCB API Log. Never raises.

	If logging itself fails, fall back to the app logger so the underlying
	business operation is not affected by an audit-side problem.
	"""
	try:
		if sales_order and not frappe.db.exists("Sales Order", sales_order):
			sales_order = ""
		if plot_contract and not frappe.db.exists("Plot Contract", plot_contract):
			plot_contract = ""
		if payment_entry and not frappe.db.exists("Payment Entry", payment_entry):
			payment_entry = ""
		doc = frappe.get_doc({
			"doctype":            "TCB API Log",
			"requested_at":       now(),
			"response_at":        now(),
			"direction":          direction,
			"event_type":         event_type,
			"status":             status,
			"processing_mode":    processing_mode or "",
			"is_duplicate":       cint(is_duplicate),
			"endpoint":           endpoint or "",
			"http_status_code":   http_status_code if http_status_code is not None else None,
			"tcb_status_code":    tcb_status_code if tcb_status_code is not None else None,
			"tcb_message":        (tcb_message or "")[:140],
			"external_reference": external_reference or "",
			"transaction_id":     transaction_id or "",
			"sales_order":        sales_order or "",
			"plot_contract":      plot_contract or "",
			"payment_entry":      payment_entry or "",
			"request_payload":    _to_pretty_json_text(request_payload),
			"response_payload":   _to_pretty_json_text(response_payload),
			"error":              error or "",
		})
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.logger("landms").error(
			"Failed to insert TCB API Log",
			exc_info=True,
		)
		return None


def has_duplicate_ipn(transaction_id: str, reference: str) -> bool:
	"""Idempotency guard for IPN callbacks."""
	if not transaction_id or not reference:
		return False
	try:
		return bool(
			frappe.db.exists(
				"TCB API Log",
				{
					"direction": "Inbound",
					"event_type": "IPN Callback",
					"transaction_id": transaction_id,
					"external_reference": reference,
				},
			)
		)
	except Exception:
		return False


def _record_registry_event(control_number: str, event_type: str, event_status: str,
                           log_name: str | None, note: str | None = None) -> None:
	"""Append a non-state-changing event to the registry trail."""
	registry = _get_registry(control_number)
	if registry:
		try:
			registry.append_log(log_name, event_type, event_status, note=note)
		except Exception:
			frappe.logger("landms").error(
				"Failed to append registry log",
				exc_info=True,
			)


# ---------------------------------------------------------------------- #
#  HTTP helpers                                                            #
# ---------------------------------------------------------------------- #


def _validate_live_reference_settings(settings: dict[str, Any]):
	missing = []
	if not settings.get("api_key"):
		missing.append("API Key")
	if not settings.get("partner_code"):
		missing.append("Partner Code")
	if not settings.get("profile_id"):
		missing.append("Profile ID / Account Number")
	if missing:
		frappe.throw(
			"TCB outbound mode is Live but required integration fields are missing: "
			+ ", ".join(missing)
		)


def _reference_create_url(settings: dict[str, Any]) -> str:
	base = (settings.get("live_base_url") or "https://partners.tcbbank.co.tz").rstrip("/")
	api_key = settings.get("api_key") or ""
	return f"{base}/public/api/reference/{api_key}"


def _masked_reference_endpoint(settings: dict[str, Any]) -> str:
	base = (settings.get("live_base_url") or "https://partners.tcbbank.co.tz").rstrip("/")
	return f"{base}/public/api/reference/<API_KEY>"


def _reference_decline_url(settings: dict[str, Any]) -> str:
	base = (settings.get("live_base_url") or "https://partners.tcbbank.co.tz").rstrip("/")
	api_key = settings.get("api_key") or ""
	return f"{base}/public/api/reference/decline/{api_key}"


def _masked_reference_decline_endpoint(settings: dict[str, Any]) -> str:
	base = (settings.get("live_base_url") or "https://partners.tcbbank.co.tz").rstrip("/")
	return f"{base}/public/api/reference/decline/<API_KEY>"


def _reconciliation_url(settings: dict[str, Any]) -> str:
	base = (settings.get("reconciliation_base_url") or "https://partners.tcbbank.co.tz:8444").rstrip("/")
	api_key = settings.get("api_key") or ""
	return f"{base}/public/api/reconciliation/{api_key}"


def _masked_reconciliation_endpoint(settings: dict[str, Any]) -> str:
	base = (settings.get("reconciliation_base_url") or "https://partners.tcbbank.co.tz:8444").rstrip("/")
	return f"{base}/public/api/reconciliation/<API_KEY>"


def _fetch_reconciliation_rows(*, settings: dict[str, Any], start_date: str, end_date: str) -> dict[str, Any]:
	_validate_live_reference_settings(settings)
	url = _reconciliation_url(settings)
	endpoint = _masked_reconciliation_endpoint(settings)
	payload = {
		"partnerCode": settings.get("partner_code") or "",
		"startDate":   start_date,
		"endDate":     end_date,
	}
	verify_ssl = bool(cint(settings.get("verify_ssl", 1)))
	connect_timeout = flt(settings.get("connect_timeout_seconds") or 5)
	read_timeout = flt(settings.get("read_timeout_seconds") or 15)

	http_status = None
	parsed_body = None
	tcb_status = None
	tcb_message = None
	try:
		session = get_request_session()
		response = session.post(
			url, data=payload,
			timeout=(connect_timeout, read_timeout),
			verify=verify_ssl,
		)
		http_status = response.status_code
		parsed_body = _parse_json_or_text(response)
		tcb_status, tcb_message = _extract_tcb_status_message(parsed_body)

		success_rows = []
		ok = False
		if isinstance(parsed_body, list):
			if parsed_body and isinstance(parsed_body[0], dict) and ("Status" in parsed_body[0] or "status" in parsed_body[0]):
				ok = False
			else:
				success_rows = parsed_body
				ok = response.ok
		elif isinstance(parsed_body, dict):
			if "Status" in parsed_body or "status" in parsed_body:
				ok = False
			elif parsed_body:
				success_rows = [parsed_body]
				ok = response.ok

		create_tcb_api_log(
			direction="Outbound",
			event_type="Reconciliation",
			status="Success" if ok else "Failed",
			processing_mode="Live",
			endpoint=endpoint,
			http_status_code=http_status,
			tcb_status_code=tcb_status,
			tcb_message=tcb_message,
			request_payload=payload,
			response_payload=parsed_body,
			error=None if ok else "TCB reconciliation API returned non-success status.",
		)
		if not ok:
			return {
				"ok": False,
				"message": (
					f"TCB reconciliation failed (HTTP {http_status}, TCB Status {tcb_status}: "
					f"{tcb_message or 'No message'})."
				),
				"rows": [],
			}
		return {"ok": True, "rows": success_rows}
	except Exception:
		err = frappe.get_traceback()
		create_tcb_api_log(
			direction="Outbound",
			event_type="Reconciliation",
			status="Failed",
			processing_mode="Live",
			endpoint=endpoint,
			http_status_code=http_status,
			tcb_status_code=tcb_status,
			tcb_message=tcb_message,
			request_payload=payload,
			response_payload=parsed_body,
			error=err,
		)
		return {"ok": False, "message": "TCB reconciliation request failed with exception.", "rows": []}


def _has_payment_reference_for_sales_order(so_name: str, reference_no: str) -> bool:
	if not reference_no:
		return False

	invoice_name = frappe.db.get_value("Sales Order", so_name, "plot_sales_invoice")
	if not invoice_name:
		return False
	existing = frappe.db.sql(
		"""
		select pe.name
		from `tabPayment Entry` pe
		inner join `tabPayment Entry Reference` per
			on per.parent = pe.name
		where pe.docstatus = 1
		  and pe.reference_no = %s
		  and per.reference_doctype = 'Sales Invoice'
		  and per.reference_name = %s
		limit 1
		""",
		(reference_no, invoice_name),
		as_dict=True,
	)
	return bool(existing)


def _normalize_date_string(value) -> str:
	if not value:
		return today()
	try:
		return str(get_datetime(value).date())
	except Exception:
		return today()


def _parse_json_or_text(response):
	content_type = (response.headers.get("content-type") or "").lower()
	if "json" in content_type:
		try:
			return response.json()
		except Exception:
			pass
	text = (response.text or "").strip()
	if not text:
		return {}
	try:
		return json.loads(text)
	except Exception:
		return {"raw_text": text}


def _extract_tcb_status_message(parsed_body):
	if isinstance(parsed_body, list) and parsed_body:
		row = parsed_body[0] if isinstance(parsed_body[0], dict) else {}
	elif isinstance(parsed_body, dict):
		row = parsed_body
	else:
		row = {}

	status_code = row.get("Status")
	if status_code is None:
		status_code = row.get("status")
	try:
		status_code = cint(status_code) if status_code is not None else None
	except Exception:
		status_code = None

	message = row.get("Message") or row.get("message") or ""
	return status_code, message


def _to_pretty_json_text(data):
	if data is None:
		return ""
	if isinstance(data, str):
		return data
	try:
		return json.dumps(data, default=str, indent=2, sort_keys=True)
	except Exception:
		return str(data)
