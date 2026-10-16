"""TCB integration module — control number lifecycle, outbound and inbound.

Public surface (used by sales_order_hooks, api/tcb.py, scheduler jobs):

  generate_control_number(sales_order_name)              → str
  is_valid_control_number(value, pattern=None)           → bool
  register_reference_for_sales_order(so_name, cn)        → dict
  decline_reference_for_sales_order(so_name, cn)         → dict
  apply_tcb_payment_to_sales_order(...)                  → dict
  run_tcb_reconciliation_job(start_date, end_date)       → dict
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
import time
from typing import Any
from urllib.parse import quote_plus, urlencode

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
TCB_MOBILE_PATTERN = re.compile(r"^255\d{9}$")


# ---------------------------------------------------------------------- #
#  Pattern handling                                                        #
# ---------------------------------------------------------------------- #


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


def is_valid_control_number(
	value: str, pattern: str | None = None, *, related: bool = False, sales_order_name: str | None = None
) -> bool:
	"""Strict pattern check — same shape used by generation."""
	value = cstr(value).strip()
	if not value:
		return False

	if not pattern and sales_order_name:
		land_acq = frappe.db.get_value("Sales Order", sales_order_name, "land_acquisition")
		if land_acq:
			field = "related_control_number_pattern" if related else "control_number_pattern"
			pattern = frappe.db.get_value("Land Acquisition", land_acq, field)

	pattern = cstr(pattern).strip()
	if not pattern:
		return False
	return bool(_pattern_to_regex(pattern).match(value))


def is_valid_tcb_mobile(value: str | None) -> bool:
	"""Return True when the mobile number is in TCB's 255XXXXXXXXX format."""
	value = cstr(value).strip()
	if not value:
		return False
	return bool(TCB_MOBILE_PATTERN.fullmatch(value))


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

	Sources of truth:
	  - tabSales Order.control_number         (primary CN field)
	  - tabSales Order.related_control_number  (related CN field)
	  - tabTCB Control Number                  (the registry — DB-level unique by name)

	All are checked. Wrapped in try/except so a missing column on a fresh
	install can't break generation.
	"""
	try:
		if frappe.db.has_column("Sales Order", "control_number"):
			if frappe.db.exists("Sales Order", {"control_number": candidate}):
				return True
	except Exception:
		pass
	try:
		if frappe.db.has_column("Sales Order", "related_control_number"):
			if frappe.db.exists("Sales Order", {"related_control_number": candidate}):
				return True
	except Exception:
		pass
	try:
		if frappe.db.exists("TCB Control Number", candidate):
			return True
	except Exception:
		pass
	try:
		if frappe.db.has_column("TCB Control Number", "related_control_number"):
			if frappe.db.exists("TCB Control Number", {"related_control_number": candidate}):
				return True
	except Exception:
		pass
	return False


def generate_control_number(
	sales_order_name: str | None = None, *, related: bool = False, pattern: str | None = None
) -> str:
	"""Generate a unique TCB control number.

	The pattern is read from the Sales Order's associated Land Acquisition.
	The result is checked against Sales Order fields and the TCB Control Number
	registry to avoid collisions.
	"""
	if not pattern and sales_order_name:
		land_acq = frappe.db.get_value("Sales Order", sales_order_name, "land_acquisition")
		if not land_acq:
			frappe.throw(
				f"Sales Order {sales_order_name} is missing a Land Acquisition. Cannot generate TCB control number."
			)

		field = "related_control_number_pattern" if related else "control_number_pattern"
		pattern = frappe.db.get_value("Land Acquisition", land_acq, field)
		if not pattern:
			frappe.throw(
				f"Land Acquisition {land_acq} is missing {field}. "
				"Please re-save the Land Acquisition to auto-generate the pattern."
			)

	if not pattern:
		frappe.throw("Cannot generate TCB control number: pattern is missing.")

	if "#" not in pattern:
		label = "Related Control Number Pattern" if related else "Control Number Pattern"
		frappe.throw(f"{label} is missing '#'. Cannot generate randomized references.")

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


def _notify_so_owner(sales_order_name: str, message: str, indicator: str = "green"):
	"""Send a visible popup notification to the Sales Order owner."""
	try:
		user = frappe.db.get_value("Sales Order", sales_order_name, "owner") if sales_order_name else None
		if not user:
			return
		frappe.publish_realtime(
			"msgprint",
			{"message": message, "alert": True, "indicator": indicator},
			user=user,
		)
	except Exception:
		pass


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
			"auto_apply_callback_payments": 0,
			"auto_apply_reconciliation_payments": 0,
			"reconciliation_enabled": 0,
			"reconciliation_lookback_days": 1,
			"decline_reference_on_so_cancel": 1,
			"decline_failure_policy": "Allow Cancel and Flag",
			"reference_create_url": "",
			"reference_decline_url": "",
			"reconciliation_url": "",
			"verify_ssl": 1,
			"connect_timeout_seconds": 5,
			"read_timeout_seconds": 15,
			"auto_catch_up": 0,
		}

	get_value = settings_doc.get

	return {
		"enabled": get_value("enabled"),
		"outbound_mode": get_value("outbound_mode") or "Off",
		"inbound_mode": get_value("inbound_mode") or "Off",
		"auto_apply_callback_payments": get_value("auto_apply_callback_payments"),
		"auto_apply_reconciliation_payments": get_value("auto_apply_reconciliation_payments"),
		"reconciliation_enabled": get_value("reconciliation_enabled"),
		"reconciliation_lookback_days": get_value("reconciliation_lookback_days") or 1,
		"decline_reference_on_so_cancel": get_value("decline_reference_on_so_cancel"),
		"decline_failure_policy": get_value("decline_failure_policy") or "Allow Cancel and Flag",
		"reference_create_url": (get_value("reference_create_url") or "").strip(),
		"reference_decline_url": (get_value("reference_decline_url") or "").strip(),
		"reconciliation_url": (get_value("reconciliation_url") or "").strip(),
		"verify_ssl": get_value("verify_ssl"),
		"connect_timeout_seconds": get_value("connect_timeout_seconds") or 5,
		"read_timeout_seconds": get_value("read_timeout_seconds") or 15,
		# "Waiting for TCB" automatic catch-up — off unless an operator ticks it
		"auto_catch_up": get_value("auto_catch_up") or 0,
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


# ---------------------------------------------------------------------- #
#  Registry helpers                                                        #
# ---------------------------------------------------------------------- #


def create_or_get_registry(
	*,
	control_number: str,
	sales_order: str,
	customer: str | None = None,
	amount: float = 0,
	related_control_number: str = "",
):
	"""Idempotent registry creation. Returns the registry doc."""
	if frappe.db.exists("TCB Control Number", control_number):
		return frappe.get_doc("TCB Control Number", control_number)
	doc = frappe.get_doc(
		{
			"doctype": "TCB Control Number",
			"control_number": control_number,
			"sales_order": sales_order,
			"customer": customer or "",
			"amount": flt(amount),
			"related_control_number": related_control_number or "",
			"status": "Generated",
			"generated_at": now(),
			"last_event": f"Generated for {sales_order}",
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _get_registry(control_number: str):
	if not control_number:
		return None
	if not frappe.db.exists("TCB Control Number", control_number):
		return None
	return frappe.get_doc("TCB Control Number", control_number)


def never_registered(control_number: str) -> bool:
	"""True when TCB has never confirmed this number: its registry row is still
	Generated or Failed and no registration ever succeeded. With the other
	checks in payments.router (Malipopay project, TCB unreachable, no payment
	ever arrived) such a number may be stopped while TCB is down; switching it
	off at TCB is then queued under Waiting for TCB."""
	status = frappe.db.get_value("TCB Control Number", control_number, "status") if control_number else None
	if status not in ("Generated", "Failed"):
		return False
	return not frappe.db.exists(
		"TCB Control Number Log Link",
		{"parent": control_number, "event_type": "Reference Create", "status": "Success"},
	)


def payment_ever_arrived(control_number: str) -> bool:
	"""True when anything shows money was ever paid on this number at TCB — a
	payment notice or a reconciliation row naming it (even one we refused), or
	a payment recorded on its registry row. Such a number is live at TCB
	whatever our registry status says, so it must never be treated as
	"nothing to release"."""
	if not control_number:
		return False
	paid = frappe.db.get_value(
		"TCB Control Number", control_number, ["paid_amount", "payment_entry", "paid_at"], as_dict=True
	)
	if paid and (flt(paid.paid_amount) > 0 or paid.payment_entry or paid.paid_at):
		return True
	if frappe.db.exists("TCB Payment Notification", {"reference": control_number}):
		return True
	return bool(
		frappe.db.exists(
			"TCB API Log",
			{
				"direction": "Inbound",
				"event_type": ["in", ["IPN Callback", "Reconciliation"]],
				"external_reference": control_number,
			},
		)
	)


# ---------------------------------------------------------------------- #
#  Outbound — Reference Create                                             #
# ---------------------------------------------------------------------- #


def _build_reference_payload(*, control_number: str, sales_order_name: str = "") -> dict[str, Any]:
	"""Build the full outbound payload for TCB Reference Create (v0.6).

	Mandatory: partnerCode, profileID, reference, name, mobile, message.
	Optional:  relatedRef, paymentOption, expireDate.
	Sent as application/x-www-form-urlencoded POST.
	"""
	expire_date = ""
	amount = 0.0

	if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
		frappe.throw(f"Sales Order {sales_order_name} not found.")

	so = frappe.db.get_value(
		"Sales Order",
		sales_order_name,
		[
			"customer",
			"customer_name",
			"contact_mobile",
			"contact_phone",
			"related_control_number",
			"payment_option",
			"include_amount",
			"payment_deadline",
			"include_expire_date",
			"grand_total",
			"land_acquisition",
		],
		as_dict=True,
	)
	if not so.land_acquisition:
		frappe.throw(f"Sales Order {sales_order_name} must have a land acquisition.")

	project_info = frappe.db.get_value(
		"Land Acquisition",
		so.land_acquisition,
		["partner_code", "profile_id"],
		as_dict=True,
	)
	if not project_info or not project_info.partner_code or not project_info.profile_id:
		frappe.throw(f"Land Acquisition {so.land_acquisition} must have a partner code and profile id.")

	customer_name = so.customer_name or so.customer or ""
	message = f"Plot payment for {customer_name} - {control_number}"
	mobile = (
		frappe.db.get_value("Customer", so.customer, "mobile_no")
		or so.contact_mobile
		or so.contact_phone
		or _get_contact_mobile(so.customer)
		or "0"
	)
	related_ref = cstr(so.related_control_number or "").strip()
	payment_option = cstr(so.payment_option or "").strip()
	if cint(so.include_expire_date) and so.payment_deadline:
		expire_date = cstr(so.payment_deadline).strip()
	if cint(so.include_amount):
		amount = flt(so.grand_total)

	payload = {
		"partnerCode": project_info.get("partner_code") or "",
		"profileID": project_info.get("profile_id") or "",
		"reference": control_number,
		"name": customer_name,
		"mobile": mobile,
		"message": message,
	}
	if related_ref:
		payload["relatedRef"] = related_ref
	if payment_option:
		payload["paymentOption"] = payment_option
	if expire_date:
		payload["expireDate"] = expire_date
	if amount > 0:
		payload["amount"] = amount
	return payload


def _get_contact_mobile(customer: str) -> str:
	"""Return the first non-empty mobile/phone from contacts linked to a Customer."""
	contacts = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
		fields=["parent"],
		limit=5,
	)
	for c in contacts:
		mob = frappe.db.get_value("Contact", c.parent, "mobile_no")
		if mob:
			return mob
		phone = frappe.db.get_value("Contact", c.parent, "phone")
		if phone:
			return phone
	return ""


def register_reference_for_sales_order(sales_order_name: str, control_number: str) -> dict[str, Any]:
	"""Register a control number with TCB. Mode-aware (Off / Log Only / Live)."""
	settings = _get_tcb_settings()
	outbound_mode = settings.get("outbound_mode") or "Off"
	enabled = cint(settings.get("enabled"))

	payload = _build_reference_payload(control_number=control_number, sales_order_name=sales_order_name)
	endpoint = _masked_reference_endpoint(settings)

	plot_contract = ""
	if sales_order_name and frappe.db.exists("Sales Order", sales_order_name):
		plot_contract = frappe.db.get_value("Sales Order", sales_order_name, "plot_contract") or ""
	related_ref = payload.get("relatedRef") or ""

	if not enabled or outbound_mode == "Off":
		log_name = create_tcb_api_log(
			direction="Outbound",
			event_type="Reference Create",
			status="Ignored",
			processing_mode="Off",
			endpoint=endpoint,
			external_reference=control_number,
			related_ref=related_ref,
			sales_order=sales_order_name,
			plot_contract=plot_contract,
			request_payload=payload,
			response_payload={
				"message": "Outbound reference registration skipped (integration disabled or mode Off)."
			},
		)
		_record_registry_event(
			control_number, "Reference Create", "Ignored", log_name, note="Outbound mode Off — skipped."
		)
		return {
			"ok": True,
			"mode": "Off",
			"message": "TCB outbound mode is Off; reference registration skipped.",
		}

	if outbound_mode == "Log Only":
		log_name = create_tcb_api_log(
			direction="Outbound",
			event_type="Reference Create",
			status="Ignored",
			processing_mode="Log Only",
			endpoint=endpoint,
			external_reference=control_number,
			related_ref=related_ref,
			sales_order=sales_order_name,
			plot_contract=plot_contract,
			request_payload=payload,
			response_payload={"message": "Outbound Log Only mode — no call sent to TCB."},
		)
		_record_registry_event(
			control_number,
			"Reference Create",
			"Ignored",
			log_name,
			note="Outbound mode Log Only — no live call.",
		)
		return {
			"ok": True,
			"mode": "Log Only",
			"message": "TCB outbound mode is Log Only; no live call was made.",
		}

	# ALREADY REGISTERED — nothing to do. Return success so the register-first gate
	# on SO submit (and re-submits after a blocked attempt) passes straight through
	# instead of re-calling TCB on a number that is already live at the bank.
	registry = _get_registry(control_number)
	if registry and registry.status == "Registered":
		return {
			"ok": True,
			"status": "Already Registered",
			"message": f"Control number {control_number} is already registered.",
		}

	# --- Live ---
	_validate_live_reference_settings(settings, need="reference")
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
		# TCB Reference Create expects application/x-www-form-urlencoded
		# (NOT JSON). We build the body ourselves so the Content-Type header
		# is unambiguous and we don't depend on requests' auto-detection.
		encoded_body = urlencode(payload, doseq=True, quote_via=quote_plus)
		response = session.post(
			url,
			data=encoded_body,
			headers={
				"Content-Type": "application/x-www-form-urlencoded",
				"Accept": "application/json",
			},
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
			related_ref=related_ref,
			sales_order=sales_order_name,
			plot_contract=plot_contract,
			request_payload=payload,
			response_payload=parsed_body,
			error=None if ok else "TCB reference API returned non-success status.",
		)

		registry = _get_registry(control_number)
		if registry:
			if ok:
				registry.mark_registered(log_name=log_name, note=tcb_message or "Registered with TCB.")
				# Durably record the successful bank call NOW, so a later rollback
				# (e.g. a throw in the SO's on_submit steps that run after this) cannot
				# erase it. Without this the registry would revert to 'Generated' while
				# the number is live at the bank, and a retry would register a SECOND
				# number. Committing on success makes the already-registered short-circuit
				# on retry trustworthy. Only commit on success — a failure has nothing
				# live at the bank to remember.
				frappe.db.commit()
			else:
				registry.mark_failed(
					log_name=log_name,
					event_type="Reference Create",
					note=f"HTTP {http_status} TCB {tcb_status}: {tcb_message}",
				)

		if not ok:
			_notify_so_owner(
				sales_order_name,
				f"TCB reference registration failed for <b>{control_number}</b>: {tcb_message or 'No message'}",
				"red",
			)
			return {
				"ok": False,
				"mode": "Live",
				"message": (
					f"TCB reference registration failed "
					f"(HTTP {http_status}, TCB Status {tcb_status}: {tcb_message or 'No message'})."
				),
			}

		if ok:
			_notify_so_owner(
				sales_order_name,
				f"TCB reference <b>{control_number}</b> registered successfully.",
				"green",
			)

		return {
			"ok": True,
			"mode": "Live",
			"message": tcb_message or "TCB reference registered successfully.",
			"http_status": http_status,
			"tcb_status": tcb_status,
		}
	except Exception as exc:
		traceback = frappe.get_traceback()
		exc_short = str(exc)[:300]
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
			related_ref=related_ref,
			sales_order=sales_order_name,
			plot_contract=plot_contract,
			request_payload=payload,
			response_payload=parsed_body,
			error=traceback,
		)
		registry = _get_registry(control_number)
		if registry:
			registry.mark_failed(
				log_name=log_name,
				event_type="Reference Create",
				note="Reference Create raised an exception. See log.",
			)

		_notify_so_owner(
			sales_order_name,
			f"TCB reference registration failed for <b>{control_number}</b>: {exc_short[:120]}",
			"red",
		)

		return {
			"ok": False,
			"mode": "Live",
			"message": f"TCB reference registration raised an exception: {exc_short}",
		}


# ---------------------------------------------------------------------- #
#  Outbound — Reference Decline                                            #
# ---------------------------------------------------------------------- #


# TCB's decline answer when it holds no live reference under the number.
TCB_STATUS_NO_SUCH_REFERENCE = 96


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

	# ALREADY DECLINED — nothing to do. Return success so the decline-first gates
	# (close / cancel / terminate) and nightly re-runs pass straight through
	# instead of re-calling TCB on a number that is already dead (TCB would reject
	# it, which would make the gate wrongly block a legitimate retry).
	registry = _get_registry(control_number)
	if registry and registry.status == "Declined":
		return {
			"ok": True,
			"status": "Already Declined",
			"message": f"Control number {control_number} is already declined.",
		}

	if settings.get("outbound_mode") != "Live":
		# Do NOT mark the registry 'Declined' here — no live call was made, so the
		# number is NOT actually dead at the bank. Marking it Declined would make the
		# already-declined short-circuit (above) wrongly skip a real decline if the
		# site is later switched to Live, leaving an orphaned, still-payable number.
		# 'Declined' must only ever mean a confirmed bank decline. Just log the skip.
		registry = _get_registry(control_number)
		if registry:
			registry.append_log(
				None,
				"Reference Decline",
				"Ignored",
				note="Outbound mode not Live — decline call skipped; number left as-is.",
			)
		return {
			"ok": True,
			"status": "Ignored",
			"message": "Outbound mode is not Live; decline call skipped.",
		}

	_validate_live_reference_settings(settings, need="decline")

	land_acquisition = (
		frappe.db.get_value("Sales Order", sales_order_name, "land_acquisition") if sales_order_name else None
	)
	if not land_acquisition:
		frappe.throw(
			f"Sales Order {sales_order_name} must have a land acquisition to decline the TCB reference."
		)

	project_info = frappe.db.get_value(
		"Land Acquisition",
		land_acquisition,
		["partner_code", "profile_id"],
		as_dict=True,
	)
	if not project_info or not project_info.partner_code or not project_info.profile_id:
		frappe.throw(f"Land Acquisition {land_acquisition} must have a partner code and profile id.")

	endpoint = _masked_reference_decline_endpoint(settings)
	url = _reference_decline_url(settings)
	payload = {
		"partnerCode": project_info.partner_code or "",
		"acctNo": project_info.profile_id or "",
		"refNo": control_number,
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
			url,
			json=payload,
			timeout=(connect_timeout, read_timeout),
			verify=verify_ssl,
		)
		http_status = response.status_code
		parsed_body = _parse_json_or_text(response)
		tcb_status, tcb_message = _extract_tcb_status_message(parsed_body)
		# 96 "Reference no does not exist": TCB holds nothing live under this
		# number — a decline that timed out but went through, or a number TCB
		# never registered. Nothing is left to release, so the stop goes ahead;
		# reading it as a failure jammed orders for good (SO-03510, SO-04965).
		# the code alone is not enough: in bank switching 96 can also mean "system
		# malfunction", so TCB's own words must say the reference does not exist
		released_before = (
			response.ok
			and tcb_status == TCB_STATUS_NO_SUCH_REFERENCE
			and "not exist" in cstr(tcb_message).lower()
		)
		ok = response.ok and (tcb_status == 0 or released_before)

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
				note = (
					f"TCB answered {TCB_STATUS_NO_SUCH_REFERENCE} (reference does not exist): "
					f"nothing left to release. {tcb_message or ''}".strip()
					if released_before
					else tcb_message or "Declined at TCB."
				)
				from landms.landms.doctype.tcb_control_number.tcb_control_number import (
					ALLOWED_TRANSITIONS,
				)

				if "Declined" not in ALLOWED_TRANSITIONS.get(registry.status, set()):
					# Paid/Declined/Expired are terminal locally (a Paid number keeps
					# its money record) and a Reopened number cannot move to Declined:
					# record the bank's answer instead of tripping the transition guard,
					# which used to throw here and block a stop the bank had accepted.
					registry.append_log(
						log_name,
						"Reference Decline",
						"Success",
						note=f"Registry left {registry.status}. {note}",
					)
				else:
					registry.mark_declined(log_name=log_name, note=note)
				# Durably record the successful bank decline NOW, so a later rollback
				# (a throw in the forfeiture JE / credit note / cleanup steps that run
				# after this) cannot erase it. Without this the registry reverts to
				# 'Registered' while the number is dead at the bank, and a retry re-declines
				# it — TCB rejects the double-decline and the order jams permanently.
				# Committing on success makes the already-declined short-circuit trustworthy.
				frappe.db.commit()
			else:
				registry.append_log(
					log_name,
					"Reference Decline",
					"Failed",
					note=f"HTTP {http_status} TCB {tcb_status}: {tcb_message}",
				)

		if released_before:
			frappe.msgprint(
				f"TCB reference <b>{control_number}</b> was already released at the bank.",
				indicator="green",
				alert=True,
			)
			return {
				"ok": True,
				"status": "Already Released",
				"message": f"TCB holds nothing live under {control_number}; nothing to release.",
			}
		if ok:
			frappe.msgprint(
				f"TCB reference <b>{control_number}</b> declined successfully.",
				indicator="green",
				alert=True,
			)
			return {"ok": True, "status": "Success", "message": tcb_message or "TCB reference declined."}

		block_cancel = policy == "Block Cancel"
		frappe.msgprint(
			f"TCB decline failed for <b>{control_number}</b>: {tcb_message or 'No message'}",
			indicator="red",
			alert=True,
		)
		return {
			"ok": False,
			"status": "Failed",
			"block_cancel": block_cancel,
			# no answer we can read, or a server error: TCB did not decide anything
			"unreachable": not http_status or http_status >= 500,
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
			registry.append_log(
				log_name, "Reference Decline", "Failed", note="Reference Decline raised an exception."
			)
		block_cancel = policy == "Block Cancel"
		frappe.msgprint(
			f"TCB decline failed for <b>{control_number}</b>. Check TCB API Log.",
			indicator="red",
			alert=True,
		)
		return {
			"ok": False,
			"status": "Failed",
			"block_cancel": block_cancel,
			"unreachable": True,  # no answer (timeout, connection refused)
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
	hold_for_review: bool = False,
) -> dict[str, Any]:
	"""Apply a confirmed TCB payment to the matching Sales Order's plot SI.

	Looks up the SO by control_number, then walks to its plot_sales_invoice
	(populated by Plot Contract on first advance) and creates a Payment
	Entry against it. The registry is updated to Paid on success.

	When `hold_for_review` is True the Payment Entry is created but left in
	Draft (not submitted) and the registry is NOT marked paid — a human must
	review and submit it manually. Used when the IPN passed the payload
	checks but failed an auxiliary signal like the IP allowlist.

	Idempotent: if a Payment Entry with the same reference_no already
	exists for the invoice, returns {status: 'Ignored'}.
	"""
	from landms.payment_sync import record_payment_waiting_for_others

	control_number = cstr(control_number).strip()
	payment_reference = cstr(payment_reference).strip()
	amount = flt(amount)
	payment_date = _normalize_date_string(payment_date)

	if not control_number:
		return {
			"ok": False,
			"status": "Failed",
			"message": "Missing control/reference number from TCB payload.",
		}
	if amount <= 0:
		return {"ok": False, "status": "Failed", "message": "TCB payment amount must be greater than zero."}

	# Registry guard: the control number must exist in the TCB Control Number
	# registry. Payments for unknown references are rejected outright — we
	# never issued this control number, so it cannot be ours.
	if not frappe.db.exists("TCB Control Number", control_number):
		return {
			"ok": False,
			"status": "Failed",
			"message": f"Control number {control_number} is not in the TCB Control Number registry.",
		}

	standard_so_name = ""
	try:
		if frappe.db.has_column("Sales Order", "control_number"):
			# Exclude Closed/Cancelled orders: a Closed plot SO keeps docstatus=1, so a
			# docstatus-only match would let a late payment book against a forfeited/closed
			# order (e.g. during the brief window when a reopen has re-registered the number
			# but not yet finished, or a stray payment on a dead number). Only an active
			# order may receive a payment.
			standard_so_name = frappe.db.get_value(
				"Sales Order",
				{
					"control_number": control_number,
					"docstatus": 1,
					"status": ["not in", ["Closed", "Cancelled"]],
				},
				"name",
			)
	except Exception:
		standard_so_name = ""

	if not standard_so_name:
		return {
			"ok": False,
			"status": "Failed",
			"message": f"No active Sales Order was found for control number {control_number} (it may be closed or cancelled).",
		}

	reference_no = payment_reference or control_number
	if _has_payment_reference_for_sales_order(standard_so_name, reference_no):
		return {
			"ok": True,
			"status": "Ignored",
			"message": f"Payment reference {reference_no} already exists for Sales Order {standard_so_name}.",
			"sales_order": standard_so_name,
		}

	bank_account = frappe.db.get_single_value("LandMS Settings", "tcb_bank_account")
	if not bank_account:
		return {
			"ok": False,
			"status": "Failed",
			"message": "LandMS Settings is missing TCB Bank Account; cannot auto-apply payment.",
			"sales_order": standard_so_name,
		}

	# A payment that fails half-way (e.g. the government-share entry is refused)
	# must leave nothing behind: the next log write would otherwise save a
	# Payment Entry with no government share and no order/contract update (the
	# 7 Sep 2026 "zombie" payments). Undo only back to here, so the notice record
	# made before this point stays as the audit trail.
	frappe.db.savepoint(_PAYMENT_SAVEPOINT)
	try:
		# waits for, then records after, any other payment on this order at the same moment
		pe_name = record_payment_waiting_for_others(
			sales_order_name=standard_so_name,
			amount=amount,
			payment_date=payment_date,
			bank_account=bank_account,
			reference_no=reference_no,
			remarks=f"TCB Payment — {standard_so_name} / Control No: {control_number}",
			submit=not hold_for_review,
		)
	except Exception as exc:
		_undo_this_payment()
		msg = cstr(exc)
		if "Duplicate payment reference" in msg:
			return {
				"ok": True,
				"status": "Ignored",
				"message": msg,
				"sales_order": standard_so_name,
			}
		return {
			"ok": False,
			"status": "Failed",
			"message": msg or "Failed to auto-apply TCB payment.",
			"sales_order": standard_so_name,
			"error": frappe.get_traceback(),
		}

	if hold_for_review:
		# Draft PE — do NOT mark registry as paid. The money is not yet
		# booked; a reviewer must submit the PE first.
		return {
			"ok": True,
			"status": "Success",
			"message": (
				f"Payment Entry {pe_name} created in Draft for {standard_so_name} "
				"— pending manual review before submission."
			),
			"sales_order": standard_so_name,
			"payment_entry": pe_name,
		}

	registry = _get_registry(control_number)
	if registry:
		try:
			registry.mark_paid(
				payment_entry=pe_name,
				payment_reference=reference_no,
				paid_amount=amount,
				note=f"Auto-applied to {standard_so_name}.",
			)
		except Exception:
			# the payment is booked; only the number's status could not follow —
			# unless the database rolled the whole work back (a deadlock does),
			# in which case nothing is booked and that must be said
			frappe.log_error(
				frappe.get_traceback(),
				f"TCB: {pe_name} booked but control number {control_number} not marked Paid",
			)
			if not frappe.db.exists("Payment Entry", pe_name):
				return {
					"ok": False,
					"status": "Failed",
					"message": f"The payment to {standard_so_name} was undone by the database; nothing is booked.",
					"sales_order": standard_so_name,
				}
	return {
		"ok": True,
		"status": "Success",
		"message": f"Payment auto-applied to {standard_so_name}.",
		"sales_order": standard_so_name,
		"payment_entry": pe_name,
	}


_PAYMENT_SAVEPOINT = "landms_tcb_payment"


def _undo_this_payment():
	"""Undo the failed payment's own writes, keeping everything before it."""
	try:
		frappe.db.rollback(save_point=_PAYMENT_SAVEPOINT)
	except Exception:
		# a commit between attempts (record_payment_waiting_for_others) has
		# already saved everything before this attempt and dropped the
		# savepoint: a plain rollback now undoes only this attempt
		frappe.db.rollback()


# ---------------------------------------------------------------------- #
#  Reconciliation pull                                                     #
# ---------------------------------------------------------------------- #


def _row_reference(row) -> str:
	"""TCB sends the literal string 'None' when a credit carried no control number."""
	reference = cstr(row.get("reference") or "").strip()
	return "" if reference.lower() in ("", "none", "null") else reference


def _pattern_regex(pattern: str):
	"""'99910#GVA####' -> ^99910\\dGVA\\d{4}$ — '#' is one digit, everything else literal."""
	return re.compile("^" + "".join(r"\d" if ch == "#" else re.escape(ch) for ch in cstr(pattern)) + "$")


def _recon_projects(land_acquisition: str | None = None) -> list:
	"""The projects a reconciliation run covers: each with the TCB account its
	statement lives in and its numbering patterns (used for attribution)."""
	filters: dict[str, Any] = {"docstatus": ["<", 2]}
	if land_acquisition:
		filters["name"] = land_acquisition
	projects = []
	for la in frappe.get_all(
		"Land Acquisition",
		filters=filters,
		fields=[
			"name",
			"acquisition_name",
			"reconciliation_id",
			"partner_code",
			"control_number_pattern",
			"related_control_number_pattern",
		],
		order_by="name",
	):
		account = cstr(la.reconciliation_id or la.partner_code or "").strip()
		if not account:
			continue
		projects.append(
			frappe._dict(
				name=la.name,
				label=la.acquisition_name or la.name,
				account=account,
				patterns=[
					_pattern_regex(p)
					for p in (la.control_number_pattern, la.related_control_number_pattern)
					if p
				],
			)
		)
	return projects


def _attribute_recon_row(row, account: str, account_projects: list, projects: list) -> str | None:
	"""Which project a statement row belongs to — decided on OUR side, never by
	what TCB shares. Order carrying the number → registry → the single project
	whose numbering pattern fits. A credit with no control number belongs to
	the account's project only when that account is the project's alone."""
	reference = _row_reference(row)
	if reference:
		la = frappe.db.get_value(
			"Sales Order", {"control_number": reference, "docstatus": 1}, "land_acquisition"
		)
		if la:
			return la
		so_name = frappe.db.get_value("TCB Control Number", reference, "sales_order")
		if so_name:
			la = frappe.db.get_value("Sales Order", so_name, "land_acquisition")
			if la:
				return la
		fits = [p.name for p in projects if any(rx.match(reference) for rx in p.patterns)]
		return fits[0] if len(fits) == 1 else None
	return account_projects[0] if len(account_projects) == 1 else None


def run_tcb_reconciliation_job(
	start_date: str | None = None,
	end_date: str | None = None,
	triggered_by: str = "Manual",
	log_name: str | None = None,
	land_acquisition: str | None = None,
) -> dict[str, Any]:
	"""Pull reconciliation rows from TCB, attribute every row to a PROJECT on our
	side, and optionally auto-apply missing payments.

	Attribution never depends on what TCB shares between projects: a row
	belongs to the project whose order (or registry entry) carries its control
	number, else to the single project whose numbering pattern it fits. A credit
	with no control number belongs to the account's project when that account
	is the project's alone; otherwise it is 'Unmatched' — recorded with sender
	and narration for matching by hand, never a bare failure.

	Three ways to run:
	  * a log naming a Land Acquisition (form button), or land_acquisition=
	    → that project's reconciliation only: its account pulled, its rows applied;
	  * a log with no project (manual "all projects")
	    → one combined log, every row stamped with its project;
	  * no log at all (the scheduler)
	    → one log PER PROJECT, plus one log per shared account holding the
	      credits that carry no control number.
	Each distinct account is pulled once per run, however many projects share it.
	"""
	t_start = time.time()
	settings = {}
	start = ""
	end = ""
	created_logs: list[str] = []
	try:
		settings = _get_tcb_settings()
		lookback_days = cint(settings.get("reconciliation_lookback_days") or 1)
		end = _normalize_date_string(end_date)
		start = (
			_normalize_date_string(start_date)
			if start_date
			else _normalize_date_string(add_days(end, -lookback_days))
		)

		if log_name and not land_acquisition:
			land_acquisition = (
				frappe.db.get_value("TCB Reconciliation Log", log_name, "land_acquisition") or None
			)

		projects = _recon_projects(land_acquisition)
		per_project = not log_name and not land_acquisition
		if not per_project and not log_name:
			log_name = _create_recon_log(
				triggered_by=triggered_by,
				start=start,
				end=end,
				land_acquisition=land_acquisition,
				account=", ".join(sorted({p.account for p in projects})),
			)
			if log_name:
				created_logs.append(log_name)

		endpoint = _masked_reconciliation_endpoint(settings)
		run_request = {
			"action": "run",
			"triggered_by": triggered_by,
			"start_date": start,
			"end_date": end,
			"land_acquisition": land_acquisition,
		}
		create_tcb_api_log(
			direction="Outbound",
			event_type="Reconciliation",
			status="Success",
			processing_mode="Started",
			endpoint=endpoint,
			reconciliation_log=log_name,
			request_payload=run_request,
			response_payload={"message": "Reconciliation run started."},
		)

		def _refuse(msg: str, *, ok: bool = True, status: str = "Ignored") -> dict[str, Any]:
			create_tcb_api_log(
				direction="Outbound",
				event_type="Reconciliation",
				status="Failed",
				processing_mode="Started",
				endpoint=endpoint,
				reconciliation_log=log_name,
				request_payload=run_request,
				response_payload={"message": msg},
				error=msg,
			)
			_update_recon_log(
				log_name,
				status="Failed",
				message=msg,
				error=msg if not ok else "",
				duration=time.time() - t_start,
			)
			return {"ok": ok, "status": status, "message": msg}

		if not cint(settings.get("enabled")):
			return _refuse("TCB integration disabled.")
		if not cint(settings.get("reconciliation_enabled")):
			return _refuse("TCB reconciliation is disabled in settings.")
		if not projects:
			return _refuse(
				("Land Acquisition " + cstr(land_acquisition) + " has no TCB account to reconcile.")
				if land_acquisition
				else "No Land Acquisition with a TCB account found. Cannot run reconciliation.",
				ok=False,
				status="Failed",
			)

		# ---- pull each distinct account ONCE --------------------------------
		accounts: dict[str, dict[str, Any]] = {}
		for p in projects:
			accounts.setdefault(p.account, {"rows": [], "projects": []})["projects"].append(p.name)
		# An account TCB does not answer for is noted and the others go on: on
		# 15 Oct 2026 one silent account stopped every project's reconciliation.
		silent: dict[str, str] = {}
		for account, info in accounts.items():
			fetch_result = _fetch_reconciliation_rows(
				settings=settings,
				land_acquisition={"reconciliation_id": account, "partner_code": account},
				start_date=start,
				end_date=end,
				reconciliation_log=log_name,
			)
			if not fetch_result.get("ok"):
				silent[account] = fetch_result.get("message") or "TCB reconciliation fetch failed."
				continue
			info["rows"] = fetch_result.get("rows") or []
		if silent and len(silent) == len(accounts):
			err_msg = "TCB did not answer: " + "; ".join(a + " — " + m for a, m in silent.items())
			_update_recon_log(
				log_name, status="Failed", message=err_msg, error=err_msg, duration=time.time() - t_start
			)
			return {"ok": False, "status": "Failed", "message": err_msg}
		silent_note = (
			(
				" TCB did not answer for account "
				+ ", ".join(sorted(silent))
				+ ": its payments were not checked. Run it again."
			)
			if silent
			else ""
		)

		# ---- attribute every row to a project (ours to decide) --------------
		attributed: list[tuple] = []  # (row, land_acquisition or None, account)
		for account, info in accounts.items():
			for row in info["rows"]:
				attributed.append(
					(row, _attribute_recon_row(row, account, info["projects"], projects), account)
				)

		# ---- decide the scopes: (log_name or None, project or None, account label, rows)
		scopes: list[tuple] = []
		if per_project:
			for p in projects:
				scopes.append((None, p, p.account, [t for t in attributed if t[1] == p.name]))
			for account, info in accounts.items():
				if len(info["projects"]) > 1:
					loose = [t for t in attributed if t[2] == account and t[1] is None]
					if loose:
						scopes.append((None, None, account, loose))
		elif land_acquisition:
			p = projects[0]
			scopes.append(
				(
					log_name,
					p,
					p.account,
					[t for t in attributed if t[1] == p.name or (t[1] is None and t[2] == p.account)],
				)
			)
		else:
			scopes.append((log_name, None, ", ".join(sorted(accounts)), attributed))

		# ---- process each scope into its own log ----------------------------
		grand = {"rows": 0, "applied": 0, "ignored": 0, "failed": 0, "unmatched": 0}
		statuses: list[str] = []
		for scope_log, project, account_label, scoped in scopes:
			if not scope_log:
				scope_log = _create_recon_log(
					triggered_by=triggered_by,
					start=start,
					end=end,
					land_acquisition=project.name if project else None,
					account=account_label,
				)
				if scope_log:
					created_logs.append(scope_log)
			if project and project.account in silent:
				# never report "0 rows, Success" for a project TCB did not answer for
				msg = (
					str(project.label)
					+ ": not reconciled — TCB did not answer for account "
					+ project.account
					+ " ("
					+ silent[project.account]
					+ "). Run it again."
				)
				_update_recon_log(
					scope_log, status="Failed", message=msg, error=msg, duration=time.time() - t_start
				)
				statuses.append("Failed")
				continue
			result = _process_recon_scope(
				log_name=scope_log,
				project=project,
				label=(
					project.label
					if project
					else ("payments not matched to a project" if per_project else "all projects")
				),
				scoped=scoped,
				settings=settings,
				endpoint=endpoint,
				run_request=run_request,
				t_start=t_start,
			)
			if result.get("stopped"):
				return result
			if silent and not project:
				# a combined log: say which account was not checked
				status = "Partial" if result.get("status") == "Success" else result.get("status")
				_update_recon_log(
					scope_log,
					status=status,
					message=cstr(result.get("message")) + silent_note,
					total_rows=cint(result.get("rows")),
					applied=cint(result.get("applied")),
					ignored=cint(result.get("ignored")),
					failed=cint(result.get("failed")),
					unmatched=cint(result.get("unmatched")),
					duration=time.time() - t_start,
				)
				result["status"] = status
			statuses.append(result.get("status") or "Failed")
			for key in grand:
				grand[key] += cint(result.get(key))

		if not scopes:
			final_status = "Success"
		elif all(s == "Success" for s in statuses):
			final_status = "Success"
		elif all(s == "Failed" for s in statuses):
			final_status = "Failed"
		else:
			final_status = "Partial"
		if silent and final_status == "Success":
			final_status = "Partial"
		summary = (
			"Processed " + str(grand["rows"]) + " rows across " + str(len(scopes)) + " log(s): "
			"applied="
			+ str(grand["applied"])
			+ ", ignored="
			+ str(grand["ignored"])
			+ ", failed="
			+ str(grand["failed"])
			+ ", unmatched="
			+ str(grand["unmatched"])
			+ "."
			+ silent_note
		)
		create_tcb_api_log(
			direction="Outbound",
			event_type="Reconciliation",
			status="Failed" if final_status == "Failed" else "Success",
			processing_mode="Completed",
			endpoint=endpoint,
			reconciliation_log=log_name,
			request_payload=run_request,
			response_payload={
				"message": summary,
				"final_status": final_status,
				"logs": created_logs or [log_name],
				**grand,
			},
		)
		return {
			"ok": True,
			"status": final_status,
			"message": summary,
			"rows": grand["rows"],
			"applied": grand["applied"],
			"ignored": grand["ignored"],
			"failed": grand["failed"],
			"unmatched": grand["unmatched"],
			"logs": created_logs or ([log_name] if log_name else []),
			"start_date": start,
			"end_date": end,
		}
	except Exception:
		err = frappe.get_traceback()
		err_msg = "Unhandled reconciliation exception."
		for name in created_logs or [log_name]:
			_update_recon_log(
				name, status="Failed", message=err_msg, error=err, duration=time.time() - t_start
			)
		create_tcb_api_log(
			direction="Outbound",
			event_type="Reconciliation",
			status="Failed",
			processing_mode="Exception",
			endpoint=_masked_reconciliation_endpoint(settings),
			reconciliation_log=log_name,
			request_payload={
				"action": "run",
				"triggered_by": triggered_by,
				"start_date": start_date,
				"end_date": end_date,
				"land_acquisition": land_acquisition,
			},
			response_payload={"message": err_msg},
			error=err,
		)
		return {"ok": False, "status": "Failed", "message": err_msg}


def _process_recon_scope(
	*,
	log_name: str | None,
	project,
	label: str = "",
	scoped: list,
	settings: dict[str, Any],
	endpoint: str,
	run_request: dict[str, Any],
	t_start: float,
) -> dict[str, Any]:
	"""Apply (or just record) one log's worth of attributed rows. Returns the
	per-log counters and final status; 'stopped' when the operator asked."""
	applied = ignored = failed = unmatched = processed = 0

	def _stop_check():
		return _stop_reconciliation_if_requested(
			log_name=log_name,
			settings=settings,
			started_at=t_start,
			total_rows=processed,
			applied=applied,
			ignored=ignored,
			failed=failed,
		)

	stop_result = _stop_check()
	if stop_result:
		stop_result["stopped"] = True
		return stop_result

	for row, row_la, account in scoped:
		stop_result = _stop_check()
		if stop_result:
			stop_result["stopped"] = True
			return stop_result

		reference = _row_reference(row)
		amount = flt(row.get("amount"))
		transaction_id = cstr(row.get("ptid") or "").strip()
		receipt_no = cstr(row.get("receipt_no") or "").strip()
		payment_ref = receipt_no or transaction_id or reference
		payment_date = row.get("trans_date")
		sender = cstr(row.get("sender_name") or "").strip()
		narration = cstr(row.get("details") or "").strip()
		stamp = {"land_acquisition": row_la or "", "sender": sender}

		if not reference or amount <= 0:
			msg = (
				("No control number on this credit" if not reference else "Zero amount")
				+ " — account "
				+ cstr(account)
				+ (", sender " + sender if sender else "")
				+ (". Narration: " + narration if narration else "")
				+ ". Match by hand."
			)
			create_tcb_api_log(
				direction="Inbound",
				event_type="Reconciliation",
				status="Ignored",
				processing_mode="Log Only",
				endpoint=endpoint,
				reconciliation_log=log_name,
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=row,
				response_payload={"message": msg},
			)
			_append_recon_log_row(
				log_name,
				reference=reference,
				amount=amount,
				transaction_id=transaction_id,
				row_status="Unmatched",
				message=msg,
				**stamp,
			)
			unmatched += 1
			processed += 1
			continue

		if not cint(settings.get("auto_apply_reconciliation_payments")):
			msg = "Payment found on TCB but not posted — auto-apply is turned off. Review and post manually."
			create_tcb_api_log(
				direction="Inbound",
				event_type="Reconciliation",
				status="Ignored",
				processing_mode="Log Only",
				endpoint=endpoint,
				reconciliation_log=log_name,
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=row,
				response_payload={"message": msg},
			)
			_append_recon_log_row(
				log_name,
				reference=reference,
				amount=amount,
				transaction_id=transaction_id,
				row_status="Ignored",
				message=msg,
				**stamp,
			)
			ignored += 1
			processed += 1
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
			endpoint=endpoint,
			reconciliation_log=log_name,
			external_reference=reference,
			transaction_id=transaction_id,
			sales_order=result.get("sales_order"),
			payment_entry=result.get("payment_entry"),
			request_payload=row,
			response_payload={"message": result.get("message")},
			error=result.get("error"),
		)
		row_status = result.get("status")
		if row_status == "Success":
			applied += 1
			_append_recon_log_row(
				log_name,
				reference=reference,
				amount=amount,
				transaction_id=transaction_id,
				row_status="Applied",
				sales_order=result.get("sales_order") or "",
				payment_entry=result.get("payment_entry") or "",
				message=result.get("message") or "",
				**stamp,
			)
		elif row_status == "Ignored":
			ignored += 1
			_append_recon_log_row(
				log_name,
				reference=reference,
				amount=amount,
				transaction_id=transaction_id,
				row_status="Ignored",
				sales_order=result.get("sales_order") or "",
				message=result.get("message") or "",
				**stamp,
			)
		else:
			failed += 1
			_append_recon_log_row(
				log_name,
				reference=reference,
				amount=amount,
				transaction_id=transaction_id,
				row_status="Failed",
				message=result.get("message") or result.get("error") or "",
				**stamp,
			)
		processed += 1

	stop_result = _stop_check()
	if stop_result:
		stop_result["stopped"] = True
		return stop_result

	total = len(scoped)
	if failed == 0:
		final_status = "Success"
	elif applied > 0 or ignored > 0 or unmatched > 0:
		final_status = "Partial"
	else:
		final_status = "Failed"
	label = label or (project.label if project else "payments not matched to a project")
	summary = (
		str(label)
		+ ": processed "
		+ str(total)
		+ " rows: applied="
		+ str(applied)
		+ ", ignored="
		+ str(ignored)
		+ ", failed="
		+ str(failed)
		+ ", unmatched="
		+ str(unmatched)
		+ "."
	)
	_update_recon_log(
		log_name,
		status=final_status,
		message=summary,
		total_rows=total,
		applied=applied,
		ignored=ignored,
		failed=failed,
		unmatched=unmatched,
		duration=time.time() - t_start,
	)
	return {
		"status": final_status,
		"message": summary,
		"rows": total,
		"applied": applied,
		"ignored": ignored,
		"failed": failed,
		"unmatched": unmatched,
		"log": log_name,
	}


def _create_recon_log(
	triggered_by: str,
	start: str,
	end: str,
	*,
	land_acquisition: str | None = None,
	account: str | None = None,
) -> str | None:
	"""Insert a new TCB Reconciliation Log doc in Running state, stamped with the
	project it reconciles (empty = combined) and the account(s) pulled. Best-effort."""
	try:
		doc = frappe.get_doc(
			{
				"doctype": "TCB Reconciliation Log",
				"naming_series": "TCB-RECON-.YYYY.-.######",
				"status": "Running",
				"triggered_by": triggered_by,
				"land_acquisition": land_acquisition or None,
				"account": (account or "")[:140],
				"unmatched": 0,
				"started_at": now(),
				"completed_at": None,
				"duration_seconds": 0,
				"total_rows": 0,
				"applied": 0,
				"ignored": 0,
				"failed": 0,
				"date_range_start": start,
				"date_range_end": end,
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	except Exception:
		frappe.logger("landms").error("Failed to create TCB Reconciliation Log", exc_info=True)
		return None


def _update_recon_log(
	log_name: str | None,
	*,
	status: str,
	message: str = "",
	error: str = "",
	total_rows: int = 0,
	applied: int = 0,
	ignored: int = 0,
	failed: int = 0,
	unmatched: int = 0,
	duration: float = 0.0,
) -> None:
	"""Update an existing TCB Reconciliation Log after a run. Best-effort."""
	if not log_name:
		return
	try:
		frappe.db.set_value(
			"TCB Reconciliation Log",
			log_name,
			{
				"status": status,
				"completed_at": now(),
				"duration_seconds": round(duration, 2),
				"total_rows": total_rows,
				"applied": applied,
				"ignored": ignored,
				"failed": failed,
				"unmatched": unmatched,
				"message": (message or "")[:500],
				"error": error or "",
			},
			update_modified=True,
		)
		frappe.db.commit()
	except Exception:
		frappe.logger("landms").error("Failed to update TCB Reconciliation Log", exc_info=True)


def _append_recon_log_row(
	log_name: str | None,
	*,
	reference: str = "",
	amount: float = 0.0,
	transaction_id: str = "",
	row_status: str,
	sales_order: str = "",
	payment_entry: str = "",
	message: str = "",
	land_acquisition: str = "",
	sender: str = "",
) -> None:
	"""Append one detail row to the TCB Reconciliation Log child table. Best-effort."""
	if not log_name:
		return
	try:
		doc = frappe.get_doc("TCB Reconciliation Log", log_name)
		doc.append(
			"rows",
			{
				"reference": reference,
				"land_acquisition": land_acquisition or None,
				"sender": (sender or "")[:140],
				"amount": flt(amount),
				"transaction_id": transaction_id,
				"row_status": row_status,
				"sales_order": sales_order,
				"payment_entry": payment_entry,
				"message": (message or "")[:500],
			},
		)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.logger("landms").error("Failed to append TCB Reconciliation Log row", exc_info=True)


def _is_reconciliation_stop_requested(log_name: str | None) -> bool:
	if not log_name or not frappe.db.exists("TCB Reconciliation Log", log_name):
		return False
	return bool(frappe.db.get_value("TCB Reconciliation Log", log_name, "stop_requested"))


def _stop_reconciliation_if_requested(
	*,
	log_name: str | None,
	settings: dict[str, Any],
	started_at: float,
	total_rows: int,
	applied: int,
	ignored: int,
	failed: int,
) -> dict[str, Any] | None:
	if not _is_reconciliation_stop_requested(log_name):
		return None

	message = (
		f"Stopped after processing {total_rows} rows: applied={applied}, ignored={ignored}, failed={failed}."
	)
	_update_recon_log(
		log_name,
		status="Stopped",
		message=message,
		total_rows=total_rows,
		applied=applied,
		ignored=ignored,
		failed=failed,
		duration=time.time() - started_at,
	)
	create_tcb_api_log(
		direction="Outbound",
		event_type="Reconciliation",
		status="Stopped",
		processing_mode="Stopped",
		endpoint=_masked_reconciliation_endpoint(settings),
		reconciliation_log=log_name,
		request_payload={"action": "stop"},
		response_payload={"message": message},
	)
	return {
		"ok": True,
		"status": "Stopped",
		"message": message,
		"rows": total_rows,
		"applied": applied,
		"ignored": ignored,
		"failed": failed,
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
	related_ref: str | None = None,
	transaction_id: str | None = None,
	sales_order: str | None = None,
	plot_contract: str | None = None,
	payment_entry: str | None = None,
	reconciliation_log: str | None = None,
	request_payload: Any = None,
	response_payload: Any = None,
	error: str | None = None,
	is_duplicate: int = 0,
) -> str | None:
	"""Best-effort insertion into TCB API Log. Never raises."""
	try:
		if sales_order and not frappe.db.exists("Sales Order", sales_order):
			sales_order = ""
		if plot_contract and not frappe.db.exists("Plot Contract", plot_contract):
			plot_contract = ""
		if payment_entry and not frappe.db.exists("Payment Entry", payment_entry):
			payment_entry = ""
		if reconciliation_log and not frappe.db.exists("TCB Reconciliation Log", reconciliation_log):
			reconciliation_log = ""
		doc = frappe.get_doc(
			{
				"doctype": "TCB API Log",
				"naming_series": "TCB-LOG-.YYYY.-.######",
				"requested_at": now(),
				"response_at": now(),
				"direction": direction,
				"event_type": event_type,
				"status": status,
				"processing_mode": processing_mode or "",
				"is_duplicate": cint(is_duplicate),
				"endpoint": endpoint or "",
				"http_status_code": cint(http_status_code),
				"tcb_status_code": cint(tcb_status_code),
				"tcb_message": (tcb_message or "")[:140],
				"external_reference": external_reference or "",
				"related_ref": related_ref or "",
				"transaction_id": transaction_id or "",
				"sales_order": sales_order or "",
				"plot_contract": plot_contract or "",
				"payment_entry": payment_entry or "",
				"reconciliation_log": reconciliation_log or "",
				"request_payload": _to_pretty_json_text(request_payload),
				"response_payload": _to_pretty_json_text(response_payload),
				"error": error or "",
			}
		)
		doc.insert(ignore_permissions=True)
		frappe.db.commit()
		return doc.name
	except Exception:
		frappe.logger("landms").error(
			"Failed to insert TCB API Log",
			exc_info=True,
		)
		return None


def has_duplicate_ipn(transaction_id: str, reference: str) -> bool:
	"""Idempotency guard for IPN callbacks.

	A prior Failed callback should remain retryable after we fix the underlying
	problem, so only non-failed inbound logs count as duplicates.
	"""
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
					"status": ("!=", "Failed"),
				},
			)
		)
	except Exception:
		return False


def _record_registry_event(
	control_number: str, event_type: str, event_status: str, log_name: str | None, note: str | None = None
) -> None:
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


def _validate_live_reference_settings(settings: dict[str, Any], *, need: str = "reference") -> None:
	"""Validate settings before a live call.

	`need` picks which URL is mandatory:
	    reference    → reference_create_url
	    decline      → reference_decline_url
	    reconciliation → reconciliation_url
	"""
	missing = []

	url_field = {
		"reference": "reference_create_url",
		"decline": "reference_decline_url",
		"reconciliation": "reconciliation_url",
	}.get(need, "reference_create_url")

	if not settings.get(url_field):
		missing.append(url_field.replace("_", " ").title())

	if missing:
		frappe.throw(
			"TCB outbound mode is Live but required integration fields are missing: " + ", ".join(missing)
		)


def _reference_create_url(settings: dict[str, Any]) -> str:
	return settings.get("reference_create_url") or ""


def _masked_reference_endpoint(settings: dict[str, Any]) -> str:
	return _mask_url_secret(settings.get("reference_create_url") or "")


def _reference_decline_url(settings: dict[str, Any]) -> str:
	return settings.get("reference_decline_url") or ""


def _masked_reference_decline_endpoint(settings: dict[str, Any]) -> str:
	return _mask_url_secret(settings.get("reference_decline_url") or "")


def _reconciliation_url(settings: dict[str, Any]) -> str:
	return settings.get("reconciliation_url") or ""


def _masked_reconciliation_endpoint(settings: dict[str, Any]) -> str:
	return _mask_url_secret(settings.get("reconciliation_url") or "")


def _mask_url_secret(url: str) -> str:
	"""Replace the final path segment of a TCB URL with <SECRET>.

	TCB endpoints embed the partner secret as the last path component, so
	we mask that for safe logging without losing the host / route context.
	"""
	if not url:
		return ""
	try:
		head, sep, tail = url.rpartition("/")
		if sep and tail:
			return f"{head}/<SECRET>"
	except Exception:
		pass
	return url


def _fetch_reconciliation_rows(
	*,
	settings: dict[str, Any],
	land_acquisition: dict[str, Any],
	start_date: str,
	end_date: str,
	reconciliation_log: str | None = None,
) -> dict[str, Any]:
	_validate_live_reference_settings(settings, need="reconciliation")
	url = _reconciliation_url(settings)
	endpoint = _masked_reconciliation_endpoint(settings)
	reconciliation_id = (
		land_acquisition.get("reconciliation_id") or land_acquisition.get("partner_code") or ""
	)
	payload = {
		"partnerCode": reconciliation_id,
		"startDate": start_date,
		"endDate": end_date,
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
		# TCB Reconciliation expects application/x-www-form-urlencoded
		# (same as Reference Create). Build the body explicitly so the
		# Content-Type header is unambiguous.
		encoded_body = urlencode(payload, doseq=True, quote_via=quote_plus)
		response = session.post(
			url,
			data=encoded_body,
			headers={
				"Content-Type": "application/x-www-form-urlencoded",
				"Accept": "application/json",
			},
			timeout=(connect_timeout, read_timeout),
			verify=verify_ssl,
		)
		http_status = response.status_code
		parsed_body = _parse_json_or_text(response)
		tcb_status, tcb_message = _extract_tcb_status_message(parsed_body)

		success_rows = []
		ok = False
		if isinstance(parsed_body, list):
			if (
				parsed_body
				and isinstance(parsed_body[0], dict)
				and ("Status" in parsed_body[0] or "status" in parsed_body[0])
			):
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
			reconciliation_log=reconciliation_log,
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
			reconciliation_log=reconciliation_log,
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
