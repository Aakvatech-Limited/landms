# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Malipopay inbound API — the webhook receiver (their "callback").

The doorman, not the accountant. It does exactly four things: verify the
signature over the RAW bytes, file the delivery in the Malipopay Webhook
Event inbox, commit, answer 200. It NEVER books money — booking is a later
stage that works from Malipopay's itemised breakdown receipts, never from
the callback's cumulative amount.

Contract facts this honours (Lockwood QRG # LW20260811/GVA/QRG1):
- delivery is attempted ONCE, no retry -> persist FIRST, and persist in
  every mode (Off / Log Only / Apply Payment), even when the signature
  fails; only the processing_status differs. From the commit onwards a
  notification can be delayed, never lost.
- X-Malipopay-Signature: sha256=<hex> = HMAC-SHA256(secret, raw body);
  compared with hmac.compare_digest on bytes BEFORE parsing.
- amount is CUMULATIVE -> stored as a checksum only (cumulative_amount).
- transactionId is the de-duplication key (inbox autoname MPW-{transaction_id}).
- registration takes only a URL, so the signature is the only inbound
  authentication; source-IP allow-listing belongs to the reverse proxy.

Shape mirrors landms/api/tcb.py ipn_callback with one deliberate change:
there is a single write-ahead commit (right after the inbox insert) and
nothing after it can raise its way out of the request without a 200.

Known Frappe-pipeline edge (not ours to fix): a body sent with
Content-Type application/json that is NOT valid JSON dies in
frappe.app.make_form_dict (json.loads at app.py:307) before any whitelisted
method runs -> HTTP 500, nothing filed. Malipopay only ever sends valid
JSON; a non-JSON content type reaches us and is filed as Rejected.

Proven on the local bench 2026-09-08 with simulated signed deliveries:
valid -> Ignored (Log Only) / Pending (Apply Payment); resend -> Duplicate
(attempts+1); bad or missing signature -> Rejected (filed, hmac_verified 0);
unknown reference -> Held (suspense); payment.refunded -> Held;
non-JSON body -> Rejected under a NOTXN-<sha> key. One API log row per
delivery, duplicate included.
"""

import hashlib
import hmac
import json

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, flt, now

from landms.malipopay import create_malipopay_api_log, get_malipopay_settings

ENDPOINT = "/api/method/landms.api.malipopay.webhook"
SIGNATURE_HEADER = "X-Malipopay-Signature"
MAX_BODY_BYTES = 64 * 1024  # real deliveries are ~1 KB; anything bigger is abuse
KNOWN_EVENTS = ("payment.confirmed", "payment.failed", "payment.refunded")


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=600, seconds=60)
def webhook():
	request = frappe.local.request
	raw = request.get_data(cache=True) or b""
	sig_header = (request.headers.get(SIGNATURE_HEADER) or "").strip()
	source_ip = _source_ip(request)
	settings = get_malipopay_settings()

	# Switched off: a real notice is still recorded (a bill may still be live at
	# Malipopay and it never resends), but only as a short excerpt with no API
	# log, so anyone posting to this public endpoint cannot fill the disk.
	switched_off = not cint(settings.enabled)

	if len(raw) > MAX_BODY_BYTES:
		if switched_off:
			frappe.local.response["http_status_code"] = 413
			return {"ok": False, "status": "Rejected", "message": "body too large"}
		_log(
			status="Failed",
			processing_mode=settings.inbound_mode,
			request_headers=request.headers,
			body=raw[:2048],
			response={"status": "Rejected", "reason": "body too large"},
			error=f"body of {len(raw)} bytes exceeds cap",
		)
		frappe.local.response["http_status_code"] = 413
		return {"ok": False, "status": "Rejected", "message": "body too large"}

	# 1) verify over the raw bytes, before parsing
	secret = _signing_secret()
	verified, verify_note = _verify(secret, sig_header, raw)

	# 2) parse defensively — an unparseable body is still filed
	payload, parse_error = _parse(raw)
	txn_id = _transaction_id(payload, raw)
	event_type = payload.get("event") if payload.get("event") in KNOWN_EVENTS else "unknown"
	control_number = _s(payload.get("customerReference"))
	ml_reference = _s(payload.get("reference"))

	# 3) decide the filing status from settings / collection state — never from money
	collection = _resolve_collection(control_number, ml_reference) if not parse_error else None
	status, message = _classify(settings, verified, verify_note, parse_error, payload, collection)

	# 4) de-duplicate on transactionId (their contract), then persist-first + commit
	existing = frappe.db.get_value(
		"Malipopay Webhook Event",
		{"transaction_id": txn_id},
		["name", "attempts", "hmac_verified"],
		as_dict=True,
	)
	# a signed delivery for a transaction first filed unsigned (a forgery, or a
	# secret set late) replaces that row instead of being dropped as a duplicate
	upgrade = bool(existing) and verified and not cint(existing.hmac_verified)
	if existing and not upgrade:
		frappe.db.set_value(
			"Malipopay Webhook Event",
			existing.name,
			"attempts",
			cint(existing.attempts) + 1,
			update_modified=False,
		)
		frappe.db.commit()
		response = {"ok": True, "status": "Duplicate", "event": existing.name}
		if switched_off:
			return response
		_log(
			status="Ignored",
			processing_mode=settings.inbound_mode,
			request_headers=request.headers,
			body=raw if verified else raw[:UNVERIFIED_BODY_BYTES],
			response=response,
			control_number=control_number,
			ml_reference=ml_reference,
			transaction_id=txn_id,
			malipopay_collection=(collection.name if collection else None),
			error=f"duplicate transactionId — already filed as {existing.name}",
		)
		return response

	# an unsigned delivery keeps only an excerpt: enough to see what it was,
	# not enough to fill the disk
	stored = raw if verified and not switched_off else raw[:UNVERIFIED_BODY_BYTES]
	if upgrade:
		event_name = _upgrade_unverified_event(
			existing,
			event_type=event_type,
			status=status,
			message=message,
			source_ip=source_ip,
			control_number=control_number,
			ml_reference=ml_reference,
			amount=payload.get("amount"),
			reported_status=_s(payload.get("status")),
			collection=collection,
			raw=stored,
		)
	else:
		event_name = _file_event(
			txn_id=txn_id,
			event_type=event_type,
			status=status,
			message=message,
			verified=verified,
			source_ip=source_ip,
			control_number=control_number,
			ml_reference=ml_reference,
			amount=payload.get("amount"),
			reported_status=_s(payload.get("status")),
			collection=collection,
			raw=stored,
			error=parse_error or ("" if verified else verify_note),
		)
	# ---- from here on the delivery cannot be lost ----

	response = {"ok": True, "status": status, "event": event_name}
	if switched_off:
		frappe.db.commit()
		return response
	_log(
		status=("Success" if status in ("Pending", "Ignored", "Held") else "Failed"),
		processing_mode=settings.inbound_mode,
		request_headers=request.headers,
		body=stored,
		response=response,
		control_number=control_number,
		ml_reference=ml_reference,
		transaction_id=txn_id,
		malipopay_collection=(collection.name if collection else None),
		error=("" if status in ("Pending", "Ignored", "Held") else message),
	)
	if collection and verified and not parse_error:
		_touch_collection(collection.name, payload, event_name)
	if status == "Pending" and event_name and frappe.db.exists("Malipopay Webhook Event", event_name):
		# booking happens OFF the request, from the itemised breakdown receipts —
		# the doorman answers 200 first (one delivery, no retry), the worker books
		frappe.enqueue(
			"landms.malipopay.process_webhook_event",
			event_name=event_name,
			queue="short",
			enqueue_after_commit=True,
		)
	frappe.db.commit()
	return response


# ---------------------------------------------------------------------------
# pieces — each one small, each one best-effort where the contract demands it
# ---------------------------------------------------------------------------


UNVERIFIED_BODY_BYTES = 2048


def _upgrade_unverified_event(
	existing,
	*,
	event_type,
	status,
	message,
	source_ip,
	control_number,
	ml_reference,
	amount,
	reported_status,
	collection,
	raw,
):
	"""Replace an unsigned row with the signed delivery of the same transaction."""
	frappe.db.set_value(
		"Malipopay Webhook Event",
		existing.name,
		{
			"event_type": event_type,
			"processing_status": status,
			"hmac_verified": 1,
			"attempts": cint(existing.attempts) + 1,
			"source_ip": source_ip,
			"control_number": control_number,
			"ml_reference": ml_reference,
			"cumulative_amount": flt(amount) if amount is not None else 0,
			"reported_status": reported_status,
			"malipopay_collection": (collection.name if collection else None),
			"processing_message": (message or "")[:500],
			"error": "replaced an unsigned delivery of the same transaction",
			"raw_payload": raw.decode("utf-8", errors="replace"),
		},
		update_modified=True,
	)
	frappe.db.commit()
	return existing.name


def _signing_secret():
	try:
		return (
			frappe.get_cached_doc("Malipopay Settings").get_password(
				"webhook_signing_secret", raise_exception=False
			)
			or ""
		)
	except Exception:
		return ""


def _verify(secret, sig_header, raw):
	"""(verified, note). Fail CLOSED: no secret -> unverifiable; the delivery is
	still filed (as Held) so a pre-registration or mis-set secret never loses data."""
	if not secret:
		return False, "no signing secret configured — delivery unverifiable"
	if not sig_header.lower().startswith("sha256="):
		return False, "signature header missing or malformed"
	presented = sig_header.split("=", 1)[1].strip().lower().encode()
	expected = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest().encode()
	if hmac.compare_digest(presented, expected):
		return True, ""
	return False, "signature mismatch"


def _parse(raw):
	try:
		data = json.loads(raw.decode("utf-8"))
		if not isinstance(data, dict):
			return {}, "body is not a JSON object"
		return data, ""
	except Exception as exc:
		return {}, f"unparseable body: {str(exc)[:120]}"


def _transaction_id(payload, raw):
	txn = _s(payload.get("transactionId")) or _s(payload.get("transaction_id"))
	if txn:
		return txn[:140]
	# no id to de-dup on — key the row to the exact bytes so a resend is still a duplicate
	return "NOTXN-" + hashlib.sha256(raw).hexdigest()[:24]


def _resolve_collection(control_number, ml_reference):
	"""Registry-first: our control number as stored/sent, then their ML reference.
	Prefer the live epoch; fall back to the newest row of any state."""
	if control_number:
		for filters in (
			{"stored_customer_reference": control_number, "state": ("!=", "Dead")},
			{"control_number": control_number, "state": ("!=", "Dead")},
			{"stored_customer_reference": control_number},
			{"control_number": control_number},
		):
			row = frappe.db.get_value(
				"Malipopay Collection",
				filters,
				["name", "state", "maintenance_hold"],
				as_dict=True,
				order_by="creation desc",
			)
			if row:
				return row
	if ml_reference:
		return frappe.db.get_value(
			"Malipopay Collection",
			{"ml_reference": ml_reference},
			["name", "state", "maintenance_hold"],
			as_dict=True,
			order_by="creation desc",
		)
	return None


def _classify(settings, verified, verify_note, parse_error, payload, collection):
	if parse_error:
		return "Rejected", parse_error
	if not verified:
		return ("Held" if verify_note.startswith("no signing secret") else "Rejected"), verify_note
	if not collection:
		return "Held", "unknown collection for customerReference/reference — suspense until matched"
	if cint(collection.maintenance_hold):
		return "Held", "collection under maintenance hold — queued, not booked"
	if collection.state in ("Dead", "Kill Requested"):
		if payload.get("event") == "payment.failed" and _s(payload.get("status")).upper() in (
			"EXPIRED",
			"CANCELLED",
		):
			# the notice that follows our own kill — no money moved
			return "Ignored", "bill {} at Malipopay after LandMS stopped it — no money".format(
				_s(payload.get("status")).upper()
			)
		return "Held", f"collection is {collection.state} — money on a dead bill needs an operator"
	if payload.get("event") == "payment.refunded":
		return "Held", "refund event — operator review"
	if not cint(settings.enabled) or settings.inbound_mode == "Off":
		return "Held", "inbound is Off — recorded, not processed"
	if settings.inbound_mode == "Log Only":
		return "Ignored", "Log Only mode — recorded, not processed"
	return "Pending", "queued for booking from breakdown receipts"


def _file_event(
	*,
	txn_id,
	event_type,
	status,
	message,
	verified,
	source_ip,
	control_number,
	ml_reference,
	amount,
	reported_status,
	collection,
	raw,
	error,
):
	"""Persist-first with a three-tier fallback so the delivery is written
	somewhere durable in every case: inbox row -> dead-letter API log -> Error Log."""
	body_text = raw.decode("utf-8", errors="replace")
	doc = frappe.get_doc(
		{
			"doctype": "Malipopay Webhook Event",
			"transaction_id": txn_id,
			"event_type": event_type,
			"processing_status": status,
			"received_at": now(),
			"hmac_verified": 1 if verified else 0,
			"attempts": 1,
			"source_ip": source_ip,
			"control_number": control_number,
			"ml_reference": ml_reference,
			"cumulative_amount": flt(amount) if amount is not None else 0,
			"reported_status": reported_status,
			"malipopay_collection": (collection.name if collection else None),
			"processing_message": (message or "")[:500],
			"error": (error or "")[:1000],
			"raw_payload": body_text,
		}
	)
	doc.flags.ignore_permissions = True
	doc.flags.ignore_mandatory = True  # a colleague's "make it mandatory" must never lose a delivery
	doc.flags.ignore_links = True
	try:
		doc.insert()
		frappe.db.commit()
		return doc.name
	except frappe.DuplicateEntryError:
		frappe.db.rollback()
		return "MPW-" + txn_id
	except Exception:
		frappe.db.rollback()
		try:
			create_malipopay_api_log(
				direction="Inbound",
				event_type="Webhook",
				status="Failed",
				endpoint=ENDPOINT,
				control_number=control_number,
				ml_reference=ml_reference,
				transaction_id=txn_id,
				request_payload={"body": body_text},
				error="inbox insert failed:\n" + frappe.get_traceback(),
			)
			frappe.db.commit()
		except Exception:
			pass
		frappe.log_error(
			title="Malipopay webhook: inbox insert failed for " + txn_id,
			message=frappe.get_traceback() + "\n\nRAW BODY:\n" + body_text,
		)
		frappe.db.commit()
		return ""


def _touch_collection(name, payload, event_name):
	"""Display-only provider snapshot on the register row; never drives closure."""
	try:
		values = {
			"last_event": "Webhook {}: {} status={} cumulative={}".format(
				event_name, payload.get("event"), payload.get("status"), payload.get("amount")
			)[:500]
		}
		if payload.get("status"):
			values["provider_status"] = _s(payload.get("status"))[:60]
		if payload.get("amount") is not None:
			values["provider_paid_amount"] = flt(payload.get("amount"))
		frappe.db.set_value("Malipopay Collection", name, values, update_modified=False)
	except Exception:
		pass


def _log(
	*,
	status,
	processing_mode,
	request_headers,
	body,
	response,
	control_number=None,
	ml_reference=None,
	transaction_id=None,
	malipopay_collection=None,
	error=None,
):
	try:
		headers = {
			k: v
			for k, v in request_headers.items()
			if k.lower()
			in (
				"content-type",
				"content-length",
				"user-agent",
				"x-real-ip",
				"x-forwarded-for",
				"x-malipopay-signature",
				"x-request-id",
			)
		}
		create_malipopay_api_log(
			direction="Inbound",
			event_type="Webhook",
			status=status,
			processing_mode=processing_mode,
			endpoint=ENDPOINT,
			http_status_code=200,
			control_number=control_number,
			ml_reference=ml_reference,
			transaction_id=transaction_id,
			malipopay_collection=malipopay_collection,
			request_payload={"headers": headers, "body": (body or b"").decode("utf-8", errors="replace")},
			response_payload=response,
			error=error or None,
		)
	except Exception:
		frappe.log_error(title="Malipopay webhook: API log failed", message=frappe.get_traceback())


def _source_ip(request):
	real = request.headers.get("X-Real-Ip") or ""
	if real:
		return real.strip()[:60]
	forwarded = request.headers.get("X-Forwarded-For") or ""
	if forwarded:
		return forwarded.split(",")[0].strip()[:60]
	return (request.remote_addr or "")[:60]


def _s(value):
	return str(value).strip() if value is not None else ""
