"""TCB inbound HTTP endpoints — IPN callback and manual reconciliation trigger.

URL paths:

  POST /api/method/landms.api.tcb.ipn_callback
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

import hmac
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
)

IPN_ENDPOINT = "/api/method/landms.api.tcb.ipn_callback"
RUN_RECONCILIATION_ENDPOINT = "/api/method/landms.api.tcb.run_reconciliation"


# ---------------------------------------------------------------------- #
#  IPN callback                                                            #
# ---------------------------------------------------------------------- #


@frappe.whitelist(allow_guest=True)
def ipn_callback() -> dict[str, Any]:
	"""Handle inbound TCB IPN payment notifications.

	Always returns 200 with a JSON body — TCB retries on non-200 and we never
	want to look like an outage. The full raw payload is persisted to
	`TCB Payment Notification` for audit and manual replay.
	"""
	raw_payload = {}
	envelope = {}
	body = {}
	reference = ""
	transaction_id = ""
	notification_name = ""

	def _finalize(
		notification_status: str,
		*,
		message: str = "",
		error: str = "",
		sales_order: str = "",
		payment_entry: str = "",
		log_name: str = "",
	) -> None:
		if not notification_name:
			return
		try:
			frappe.db.set_value(
				"TCB Payment Notification",
				notification_name,
				{
					"status": notification_status,
					"processing_message": (message or "")[:500],
					"error": error or "",
					"sales_order": sales_order or "",
					"payment_entry": payment_entry or "",
					"tcb_api_log": log_name or "",
				},
				update_modified=True,
			)
		except Exception:
			frappe.logger("landms").error(
				"Failed to update TCB Payment Notification",
				exc_info=True,
			)

	lock_name = ""
	lock_acquired = False
	try:
		raw_payload, envelope = _read_request_payload()
		# Unwrap the envelope: the real TCB body wraps payment details inside `param`.
		body = _extract_ipn_body(envelope)

		mode = get_tcb_inbound_mode()
		reference = cstr(body.get("reference") or "").strip()
		amount = flt(body.get("amount"))
		transaction_id = cstr(body.get("transaction_id") or "").strip()
		payment_date = body.get("transaction_date") or body.get("trans_date") or body.get("payment_date")
		payment_ref = transaction_id or reference

		# Serialize concurrent deliveries for the same transaction. TCB fires
		# the IPN several times within microseconds; without this lock each
		# request runs its duplicate check before any of them has committed,
		# they all pass, and we end up with multiple Payment Entries for one
		# payment. MySQL named lock; auto-released on connection close.
		lock_name, lock_acquired = _acquire_ipn_lock(transaction_id, reference)
		if lock_name and not lock_acquired:
			log_name = create_tcb_api_log(
				direction="Inbound",
				event_type="IPN Callback",
				status="Ignored",
				processing_mode=mode,
				endpoint=IPN_ENDPOINT,
				is_duplicate=1,
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=raw_payload,
				response_payload={"message": "Concurrent IPN — another worker is processing."},
			)
			return {
				"ok": True,
				"status": "Ignored",
				"message": "Concurrent IPN — another worker is processing this transaction.",
			}

		# We just unblocked on a lock held by the predecessor worker. MySQL's
		# default REPEATABLE READ snapshots the connection at its first read;
		# without this commit, subsequent queries would still see pre-commit
		# state and the idempotency check would miss the winner's writes.
		if lock_acquired:
			frappe.db.commit()

		# 2. Persist the notification record FIRST — even if we'll ignore it.
		#    This is the canonical audit trail of what TCB sent us.
		notification_name = _upsert_payment_notification(
			envelope=envelope,
			body=body,
			raw_payload=raw_payload,
			initial_status="Received",
		)

		# 3. Off mode — log and decline.
		if mode == "Off":
			log_name = create_tcb_api_log(
				direction="Inbound",
				event_type="IPN Callback",
				status="Ignored",
				processing_mode="Off",
				endpoint=IPN_ENDPOINT,
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=raw_payload,
				response_payload={"message": "Inbound mode Off."},
			)
			_finalize("Ignored", message="Inbound mode is Off.", log_name=log_name or "")
			return {"ok": True, "status": "Ignored", "message": "Inbound mode is Off."}

		# 3b. Inbound auth token — the shared secret we generated and gave TCB.
		#     Enforce  → reject a missing/wrong token, no Payment Entry created.
		#     Log Only → log it and hold the payment as a Draft PE for review
		#                (folded into hold_for_review below) — never dropped.
		auth_ok, auth_mode, auth_message = _verify_ipn_auth_token()
		auth_hold = False
		if not auth_ok and auth_mode == "Enforce":
			log_name = create_tcb_api_log(
				direction="Inbound",
				event_type="IPN Callback",
				status="Failed",
				processing_mode=mode,
				endpoint=IPN_ENDPOINT,
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=raw_payload,
				response_payload={"message": f"IPN auth rejected: {auth_message}"},
				error=auth_message,
			)
			_finalize(
				"Failed",
				message=f"IPN auth rejected: {auth_message}",
				error=auth_message,
				log_name=log_name or "",
			)
			return {"ok": False, "status": "Rejected", "message": "IPN auth token rejected."}
		if not auth_ok:
			# Log Only — don't drop it; force manual review downstream.
			auth_hold = True

		# 4. Idempotency — duplicate IPN with same transaction_id + reference.
		if transaction_id and has_duplicate_ipn(transaction_id, reference):
			log_name = create_tcb_api_log(
				direction="Inbound",
				event_type="IPN Callback",
				status="Ignored",
				processing_mode=mode,
				endpoint=IPN_ENDPOINT,
				is_duplicate=1,
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=raw_payload,
				response_payload={"message": "Duplicate IPN — already processed."},
			)
			# Only mark Duplicate if the winner hasn't already set it to Processed.
			# Late-arriving workers (woke after the lock) would otherwise overwrite
			# the winning request's Processed status and blank the linked documents.
			current_status = (
				frappe.db.get_value("TCB Payment Notification", notification_name, "status")
				if notification_name
				else None
			)
			if current_status != "Processed":
				_finalize("Duplicate", message="Duplicate IPN — already processed.", log_name=log_name or "")
			return {"ok": True, "status": "Ignored", "message": "Duplicate IPN."}

		# 5. Log Only — log and exit.
		if mode == "Log Only":
			log_name = create_tcb_api_log(
				direction="Inbound",
				event_type="IPN Callback",
				status="Success",
				processing_mode="Log Only",
				endpoint=IPN_ENDPOINT,
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=raw_payload,
				response_payload={"message": "Log Only — payment not applied."},
			)
			_finalize("Ignored", message="Log Only mode — payment not applied.", log_name=log_name or "")
			return {"ok": True, "status": "Logged", "message": "IPN logged (Log Only mode)."}

		# 6. Apply Payment — only if auto-apply is on.
		if not is_callback_auto_apply_enabled():
			log_name = create_tcb_api_log(
				direction="Inbound",
				event_type="IPN Callback",
				status="Ignored",
				processing_mode="Apply Payment",
				endpoint=IPN_ENDPOINT,
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=raw_payload,
				response_payload={"message": "Auto-apply switch is OFF — payment not created."},
			)
			_finalize("Ignored", message="Auto-apply is off — payment not created.", log_name=log_name or "")
			return {"ok": True, "status": "Logged", "message": "IPN logged; auto-apply is off."}

		# 7. Validate minimum payload before going to the matcher.
		if not reference or amount <= 0:
			log_name = create_tcb_api_log(
				direction="Inbound",
				event_type="IPN Callback",
				status="Failed",
				processing_mode="Apply Payment",
				endpoint=IPN_ENDPOINT,
				external_reference=reference,
				transaction_id=transaction_id,
				request_payload=raw_payload,
				response_payload={"message": "Missing reference or non-positive amount."},
			)
			_finalize(
				"Failed",
				message="Missing reference or non-positive amount.",
				error="Validation failure.",
				log_name=log_name or "",
			)
			return {"ok": False, "status": "Failed", "message": "Missing reference or non-positive amount."}

		# 8. IP allowlist check — if configured and the client IP is not in the
		#    list, still apply the payment BUT leave the PE in Draft so a human
		#    can review and submit manually. The control number itself is still
		#    validated downstream (registry + SO match).
		allowed_ips = _get_allowed_tcb_ips()
		client_ip = _get_client_ip()
		ip_blocked = bool(allowed_ips) and client_ip not in allowed_ips
		# Hold as Draft PE for review if EITHER the IP is off-allowlist OR the
		# auth token failed under Log Only mode. Never drop a real payment.
		hold_for_review = ip_blocked or auth_hold
		ip_message = (
			(
				f"Client IP {client_ip or 'unknown'} is not in the TCB allowlist; "
				"PE will be created as Draft for manual review."
			)
			if ip_blocked
			else ""
		)
		if auth_hold:
			ip_message = (ip_message + " " if ip_message else "") + (
				f"IPN auth (Log Only): {auth_message}; PE held as Draft for review."
			)

		result = apply_tcb_payment_to_sales_order(
			control_number=reference,
			amount=amount,
			payment_date=payment_date,
			payment_reference=payment_ref,
			hold_for_review=hold_for_review,
		)
		if hold_for_review and result.get("ok"):
			result["message"] = (result.get("message") or "") + " " + ip_message

		log_name = create_tcb_api_log(
			direction="Inbound",
			event_type="IPN Callback",
			status=result.get("status") or ("Success" if result.get("ok") else "Failed"),
			processing_mode="Apply Payment",
			endpoint=IPN_ENDPOINT,
			external_reference=reference,
			transaction_id=transaction_id,
			sales_order=result.get("sales_order"),
			payment_entry=result.get("payment_entry"),
			request_payload=raw_payload,
			response_payload={"message": result.get("message")},
			error=result.get("error"),
		)

		final_status = {
			"Success": "Processed",
			"Ignored": "Duplicate",
			"Failed": "Failed",
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
	except Exception:
		err = frappe.get_traceback()
		log_name = create_tcb_api_log(
			direction="Inbound",
			event_type="IPN Callback",
			status="Failed",
			processing_mode="Endpoint Exception",
			endpoint=IPN_ENDPOINT,
			external_reference=reference,
			transaction_id=transaction_id,
			request_payload=raw_payload or envelope or body,
			response_payload={"message": "Unhandled exception in IPN callback."},
			error=err,
		)
		_finalize(
			"Failed",
			message="Unhandled exception in IPN callback.",
			error=err,
			log_name=log_name or "",
		)
		return {
			"ok": False,
			"status": "Failed",
			"message": "Unhandled exception while processing IPN. Check TCB API Log.",
		}
	finally:
		# Commit before releasing so the next worker waking on the lock sees
		# our TCB API Log row and short-circuits via has_duplicate_ipn.
		if lock_acquired and lock_name:
			try:
				frappe.db.commit()
			except Exception:
				frappe.logger("landms").error("IPN commit before release failed", exc_info=True)
			_release_ipn_lock(lock_name)


# ---------------------------------------------------------------------- #
#  Manual reconciliation trigger                                           #
# ---------------------------------------------------------------------- #


@frappe.whitelist()
def run_reconciliation(start_date: str | None = None, end_date: str | None = None) -> dict[str, Any]:
	"""Manually run the TCB reconciliation pull.

	Permission: System Manager only — checked explicitly because @whitelist
	without `allow_guest` allows any logged-in user otherwise.
	"""
	request_payload = {
		"start_date": start_date,
		"end_date": end_date,
		"user": frappe.session.user,
	}
	if "System Manager" not in frappe.get_roles():
		message = "Only System Manager can trigger TCB reconciliation manually."
		create_tcb_api_log(
			direction="Inbound",
			event_type="Reconciliation",
			status="Failed",
			processing_mode="Endpoint",
			endpoint=RUN_RECONCILIATION_ENDPOINT,
			request_payload=request_payload,
			response_payload={"message": message},
			error=message,
		)
		frappe.throw(message)
	try:
		result = run_tcb_reconciliation_job(
			start_date=start_date,
			end_date=end_date,
			triggered_by="Manual",
		)
	except Exception:
		create_tcb_api_log(
			direction="Inbound",
			event_type="Reconciliation",
			status="Failed",
			processing_mode="Endpoint",
			endpoint=RUN_RECONCILIATION_ENDPOINT,
			request_payload=request_payload,
			response_payload={"message": "Manual reconciliation trigger failed."},
			error=frappe.get_traceback(),
		)
		raise
	create_tcb_api_log(
		direction="Inbound",
		event_type="Reconciliation",
		status=result.get("status")
		if result.get("status") in ("Success", "Failed", "Ignored", "Stopped")
		else ("Success" if result.get("ok") else "Failed"),
		processing_mode="Endpoint",
		endpoint=RUN_RECONCILIATION_ENDPOINT,
		request_payload=request_payload,
		response_payload=result,
		error=result.get("message") if not result.get("ok") else "",
	)
	return result


# ---------------------------------------------------------------------- #
#  Helpers                                                                 #
# ---------------------------------------------------------------------- #


def _get_client_ip() -> str:
	"""Return the real client IP, peering through nginx's X-Forwarded-For.

	Behind nginx, `request.remote_addr` is always 127.0.0.1 (the upstream
	socket). The original client sits at the leftmost entry of the
	comma-separated X-Forwarded-For header.
	"""
	try:
		request = frappe.local.request
		xff = request.headers.get("X-Forwarded-For") or ""
		if xff:
			return xff.split(",")[0].strip()
		return request.remote_addr or ""
	except Exception:
		return ""


def _acquire_ipn_lock(transaction_id: str, reference: str) -> tuple[str, bool]:
	"""Claim a per-transaction lock so simultaneous IPN deliveries don't
	create duplicate Payment Entries.

	Returns (lock_name, acquired). Empty lock_name means we couldn't key
	(no transaction_id) — caller should proceed without serialization.
	Fails open on DB errors: one duplicate PE is less bad than a swallowed
	real payment.
	"""
	if not transaction_id:
		return "", True
	# MySQL named-lock identifiers are capped at 64 chars.
	lock_name = (f"tcb_ipn:{transaction_id}:{reference}")[:64]
	try:
		row = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
		acquired = bool(row and row[0] and row[0][0] == 1)
		return lock_name, acquired
	except Exception:
		frappe.logger("landms").error("IPN lock acquire failed", exc_info=True)
		return lock_name, True


def _release_ipn_lock(lock_name: str) -> None:
	if not lock_name:
		return
	try:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
	except Exception:
		frappe.logger("landms").error("IPN lock release failed", exc_info=True)


def _verify_ipn_auth_token() -> tuple[bool, str, str]:
	"""Verify the shared auth token TCB sends in the IPN request header.

	We generate the token, hand it to TCB, and TCB echoes it back on every
	IPN. Returns (ok, mode, message):
	  - ok:      True if the check passed OR is not enforced (mode Off / no token).
	  - mode:    the configured enforcement mode ("Off" / "Log Only" / "Enforce").
	  - message: human-readable detail for logging.

	Fails OPEN on any settings/read error — a broken settings read must never
	block a real payment.
	"""
	try:
		settings = frappe.get_cached_doc("TCB Integration Settings")
	except Exception:
		return True, "Off", "TCB settings unavailable — auth check skipped."

	mode = (settings.get("ipn_auth_mode") or "Off").strip()
	if mode == "Off":
		return True, "Off", "IPN auth check is Off."

	try:
		expected = settings.get_password("ipn_auth_token", raise_exception=False) or ""
	except Exception:
		expected = ""
	expected = (expected or "").strip()
	if not expected:
		# Mode is on but no token is configured yet.
		#   Enforce  → FAIL CLOSED. "Enforce" must mean "no valid token, no payment".
		#              Passing here would accept every forged callback the moment an admin
		#              selects Enforce before pasting the secret — a security fail-open on a
		#              live money endpoint. Reject; a genuine TCB payment is not lost (TCB
		#              retries, and the operator sees the rejection in the TCB API Log).
		#   Log Only → don't block; hold the payment as a Draft PE for review downstream.
		if mode == "Enforce":
			return False, mode, "IPN auth is set to Enforce but no auth token is configured — rejecting."
		return True, mode, "No IPN auth token configured — check skipped."

	# Accept the token in ANY of the configured header names — TCB may use
	# Authorization, a custom X-Auth-Token, etc. We match against every
	# candidate so we don't have to commit to one before TCB confirms.
	header_names = _parse_header_names(settings.get("ipn_auth_header"))
	any_present = False
	for header_name in header_names:
		presented = _get_request_header(header_name)
		if not presented:
			continue
		any_present = True
		# Tolerate 'Bearer <tok>' / 'Token <tok>' prefixes.
		for prefix in ("Bearer ", "Token "):
			if presented.startswith(prefix):
				presented = presented[len(prefix) :].strip()
				break
		# Compare on bytes, not str. hmac.compare_digest raises TypeError on a
		# non-ASCII str (a stray latin-1 header byte would otherwise crash the whole
		# callback and drop the payment). Encoding both to bytes compares safely.
		if hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8")):
			return True, mode, f"IPN auth token OK (header '{header_name}')."

	names_display = ", ".join(header_names)
	if not any_present:
		return False, mode, f"IPN auth token missing (checked: {names_display})."
	return False, mode, f"IPN auth token mismatch (checked: {names_display})."


def _get_request_header(name: str) -> str:
	try:
		return (frappe.local.request.headers.get(name) or "").strip()
	except Exception:
		return ""


def _parse_header_names(raw: str | None) -> list[str]:
	"""Header name(s) TCB may carry the auth token in — comma/newline separated.

	Lets us accept the token in more than one header (e.g. both Authorization
	and X-Auth-Token) so we don't have to commit to one before TCB confirms.
	Defaults to ['Authorization'].
	"""
	parts = [p.strip() for p in (raw or "").replace("\n", ",").split(",")]
	names = [p for p in parts if p]
	return names or ["Authorization"]


def _get_allowed_tcb_ips() -> list[str]:
	"""Parse the Allowed IP Addresses field from TCB Integration Settings."""
	try:
		raw = frappe.db.get_single_value("TCB Integration Settings", "allowed_ip_addresses") or ""
	except Exception:
		return []
	if not raw:
		return []
	# Accept comma- or newline-separated values, trim blanks.
	parts = [p.strip() for p in raw.replace("\n", ",").split(",")]
	return [p for p in parts if p]


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
		"method": method,
		"source_ip": source_ip,
		"query_string": query_string,
		"headers": headers,
		"body": raw_body or parsed,
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

		doc = frappe.get_doc(
			{
				"doctype": "TCB Payment Notification",
				"status": initial_status,
				"received_at": now(),
				"tcb_status_code": _safe_int(envelope.get("status")),
				"tcb_status_desc": cstr(envelope.get("statusDesc") or envelope.get("status_desc") or "")[
					:140
				],
				"transaction_id": transaction_id,
				"reference": cstr(body.get("reference") or "")[:140],
				"account_no": cstr(body.get("account_no") or "")[:140],
				"amount": flt(body.get("amount")),
				"currency": cstr(body.get("currency") or "TZS")[:20],
				"charge": flt(body.get("charge")),
				"transaction_date": _parse_tcb_datetime(
					body.get("transaction_date") or body.get("trans_date")
				),
				"phone": cstr(body.get("phone") or "")[:140],
				"description": cstr(body.get("description") or "")[:1000],
				"tcb_control_number": (
					body.get("reference")
					if body.get("reference") and frappe.db.exists("TCB Control Number", body.get("reference"))
					else ""
				),
				"raw_payload": _to_text(raw_payload),
			}
		)
		doc.insert(ignore_permissions=True)
		# Commit immediately so the audit record survives any later rollback
		# triggered by payment processing errors. The notification is the
		# canonical record of what TCB sent — it must persist regardless of
		# whether the payment itself succeeds or fails.
		frappe.db.commit()
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
	"""TCB sends `2020-12-29T15:05:21.000+0300`. Convert to a MySQL-compatible
	naive datetime string (no timezone offset) for the DATETIME column.

	Falls back to None on parse failure — the field accepts empty values.
	"""
	if not value:
		return None
	try:
		from frappe.utils import get_datetime

		dt = get_datetime(value)
		if dt is None:
			return None
		# Strip timezone info — MySQL DATETIME does not accept offsets.
		# The value is stored as received (in TCB's local time) without conversion.
		if hasattr(dt, "replace"):
			dt = dt.replace(tzinfo=None)
		return str(dt)[:19]
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
