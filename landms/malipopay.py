# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Malipopay outbound client — collection create/adopt with full API logging.

Mirrors the shape of landms/tcb.py (settings-driven modes, per-call audit
rows, defensive parsing) with one deliberate difference, learned from the
TCB scars: NOTHING here ever commits inside a caller's transaction.
create_malipopay_api_log is best-effort insert only; explicit commits happen
solely inside the background dispatcher at its own write-ahead points.

Wiring status (2026-09-08): Sales Order on_submit/on_cancel hooks are wired
(on_sales_order_submit / on_sales_order_cancel at the bottom of this file) —
guarded to plot sales orders on Land Acquisitions with Collect via Malipopay
ticked, and inert unless Malipopay Settings is enabled with outbound_mode
Log Only/Live. With settings Off (the shipped default) the live site still
cannot reach the HTTP paths by accident.

Field gotchas proven live 2026-09-07/08 (first collection ML228116):
- their query endpoint matches the customerReference EXACTLY AS STORED, which
  is exactly as SENT (the test bill was created with "999GVATEST0002" and
  stored verbatim — an earlier reading that Malipopay adds a 999 prefix was
  WRONG, corrected by the operator 2026-09-08). The query helper still tries
  a 999-prefixed fallback as harmless robustness, never as doctrine.
- create is NOT idempotent -> ALWAYS query-before-create (the dispatcher
  refuses to blind-POST after a timeout; the stuck "Creating" state is the
  evidence the repair path needs).
- payload rules (Lockwood's corrected shape, 2026-10-01): integer TZS,
  service "mobile", no provider, one-line description. The v1.1 document's
  service "account" + provider "CRDB BANK" refused payment on 2026-09-29
  (code 91050). Contract v1.2 (2026-10-13) says the no-prompt flag is the
  boolean `push` — the `pushUSSD` text field we sent before is ignored and
  the prompt still fires — and asks for "account" + "CRDB BANK" again; that
  last point is open with Lockwood, so service/provider are unchanged.
- closing a bill (contract v1.2 §12.2): POST /api/v1/payment/{ML}/status with
  status SUSPENDED / PROCESSING / EXPIRED. The /payment/expire path of v1.0
  was never released.
"""

import json
import re
from datetime import datetime, timezone

import frappe
from frappe import _
from frappe.utils import (
	add_to_date,
	cint,
	convert_utc_to_system_timezone,
	cstr,
	flt,
	get_datetime,
	get_request_session,
	getdate,
	now,
	now_datetime,
	today,
)

CREATE_PATH = "/api/v2/payment/collection"
QUERY_PATH = "/api/v1/payment"
BREAKDOWN_PATH = "/api/v1/payment/breakdown/"
VERIFY_PATH = "/api/v1/payment/verify/"

# Their section-13 statuses on which a bill can still receive money.
PAYABLE_STATUSES = ("PROCESSING", "PARTIAL", "FAILED", "REJECTED", "USER_REJECTED")


# ---------------------------------------------------------------------------
# Settings + logging
# ---------------------------------------------------------------------------


def get_malipopay_settings():
	"""Defensive read of Malipopay Settings — a missing/broken single degrades
	to disabled instead of raising (same doctrine as _get_tcb_settings)."""
	try:
		doc = frappe.get_cached_doc("Malipopay Settings")
		token = doc.get_password("api_token", raise_exception=False)
		return frappe._dict(
			enabled=cint(doc.get("enabled")),
			outbound_mode=(doc.get("outbound_mode") or "Off"),
			inbound_mode=(doc.get("inbound_mode") or "Off"),
			base_url=(doc.get("base_url") or "").rstrip("/"),
			api_token=token or "",
			amount_type=(doc.get("collection_amount_type") or "FLEXIBLE"),
			verify_ssl=cint(doc.get("verify_ssl") if doc.get("verify_ssl") is not None else 1),
			connect_timeout=flt(doc.get("connect_timeout_seconds") or 10),
			read_timeout=flt(doc.get("read_timeout_seconds") or 30),
		)
	except Exception:
		return frappe._dict(
			enabled=0,
			outbound_mode="Off",
			inbound_mode="Off",
			base_url="",
			api_token="",
			amount_type="FLEXIBLE",
			verify_ssl=1,
			connect_timeout=10,
			read_timeout=30,
		)


def create_malipopay_api_log(
	*,
	direction,
	event_type,
	status,
	processing_mode=None,
	endpoint=None,
	http_status_code=None,
	control_number=None,
	ml_reference=None,
	transaction_id=None,
	sales_order=None,
	malipopay_collection=None,
	payment_entry=None,
	request_payload=None,
	response_payload=None,
	error=None,
):
	"""Best-effort audit row. NEVER raises and — unlike TCB's logger — NEVER
	commits: the mid-transaction commit inside create_tcb_api_log is the
	reason the TCB gates had to move into before_submit; that constraint is
	deliberately kept out of this rail. Callers own their commit points."""
	try:
		doc = frappe.get_doc(
			{
				"doctype": "Malipopay API Log",
				"direction": direction,
				"event_type": event_type,
				"status": status,
				"processing_mode": processing_mode,
				"endpoint": (endpoint or "")[:255],
				"http_status_code": cint(http_status_code) if http_status_code is not None else None,
				"control_number": control_number,
				"ml_reference": ml_reference,
				"transaction_id": transaction_id,
				"sales_order": sales_order
				if sales_order and frappe.db.exists("Sales Order", sales_order)
				else None,
				"malipopay_collection": malipopay_collection
				if malipopay_collection and frappe.db.exists("Malipopay Collection", malipopay_collection)
				else None,
				"payment_entry": payment_entry
				if payment_entry and frappe.db.exists("Payment Entry", payment_entry)
				else None,
				"requested_at": now(),
				"response_at": now(),
				"request_payload": _pretty(request_payload),
				"response_payload": _pretty(response_payload),
				"error": error,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(frappe.get_traceback(), "LandMS: Malipopay API Log insert failed")
		return None


def _pretty(payload):
	if payload is None:
		return None
	if isinstance(payload, str):
		return payload
	try:
		return json.dumps(payload, default=str, indent=2, sort_keys=True)
	except Exception:
		return str(payload)


def _headers(settings):
	return {
		"apiToken": settings.api_token,
		"Content-Type": "application/json",
		"Accept": "application/json",
	}


def _parse_json_or_text(response):
	try:
		return response.json()
	except Exception:
		return {"raw_text": (response.text or "")[:5000]}


# ---------------------------------------------------------------------------
# Read calls
# ---------------------------------------------------------------------------


def query_by_customer_reference(reference, settings=None, *, log_links=None):
	"""GET /api/v1/payment?customerReference=... — the safe 'did my collection
	actually get created?' oracle (their contract, section 8).

	Handles the proven 999-prefix behaviour: if the as-sent form returns no
	documents and the reference does not already start with 999, the
	prefixed form is tried too. Returns
	{ok, docs, queried_as, http_status, raw} — docs may be [].
	"""
	settings = settings or get_malipopay_settings()
	log_links = log_links or {}
	session = get_request_session()
	tried = [reference]
	if not str(reference).startswith("999"):
		tried.append("999" + str(reference))

	last = frappe._dict(ok=False, docs=[], queried_as=None, http_status=None, raw=None)
	all_ok = True  # False if ANY form of the reference could not be asked about
	for candidate in tried:
		url = settings.base_url + QUERY_PATH
		try:
			response = session.get(
				url,
				params={"customerReference": candidate, "limit": 10},
				headers=_headers(settings),
				timeout=(settings.connect_timeout, settings.read_timeout),
				verify=bool(settings.verify_ssl),
			)
			body = _parse_json_or_text(response)
			docs = (body.get("data") or {}).get("docs") or [] if isinstance(body, dict) else []
			create_malipopay_api_log(
				direction="Outbound",
				event_type="Collection Query",
				status="Success" if response.ok else "Failed",
				processing_mode="Live",
				endpoint=url + "?customerReference=" + candidate,
				http_status_code=response.status_code,
				control_number=reference,
				request_payload={"customerReference": candidate},
				response_payload=body,
				**log_links,
			)
			answered = bool(response.ok and isinstance(body, dict) and body.get("success") is not False)
			all_ok = all_ok and answered
			last = frappe._dict(
				ok=bool(response.ok),
				docs=docs,
				queried_as=candidate,
				http_status=response.status_code,
				raw=body,
				all_ok=all_ok,
			)
			if docs:
				return last
		except Exception:
			create_malipopay_api_log(
				direction="Outbound",
				event_type="Collection Query",
				status="Failed",
				processing_mode="Live",
				endpoint=url,
				control_number=reference,
				request_payload={"customerReference": candidate},
				error=frappe.get_traceback(),
				**log_links,
			)
			all_ok = False
			last = frappe._dict(
				ok=False, docs=[], queried_as=candidate, http_status=None, raw=None, all_ok=False
			)
	last.all_ok = all_ok
	return last


# ---------------------------------------------------------------------------
# The dispatcher — create or adopt (background-job context; owns its commits)
# ---------------------------------------------------------------------------


def create_or_adopt_collection(collection_name):
	"""Take a Malipopay Collection row (Pending Create / Creating / Create
	Failed) and converge it to Active: adopt the bill if Malipopay already
	holds a payable one for this reference (crash/timeout recovery — their
	create is NOT idempotent), else create it. Write-ahead 'Creating' state
	is committed before the HTTP call so a crash leaves evidence, never a
	mystery. Failure never raises — the row carries the outcome."""
	settings = get_malipopay_settings()
	doc = frappe.get_doc("Malipopay Collection", collection_name)

	if doc.state not in ("Pending Create", "Creating", "Create Failed"):
		return frappe._dict(ok=False, status="Ignored", message=f"state is {doc.state} — nothing to do")
	if not settings.enabled or not settings.api_token:
		create_malipopay_api_log(
			direction="Outbound",
			event_type="Collection Create",
			status="Ignored",
			processing_mode="Disabled",
			control_number=doc.control_number,
			malipopay_collection=doc.name,
			sales_order=doc.sales_order,
			response_payload={"reason": "Malipopay Settings disabled or token missing"},
		)
		return frappe._dict(ok=False, status="Ignored", message="Malipopay disabled/unconfigured")
	if settings.outbound_mode != "Live":
		create_malipopay_api_log(
			direction="Outbound",
			event_type="Collection Create",
			status="Ignored",
			processing_mode=settings.outbound_mode,
			control_number=doc.control_number,
			malipopay_collection=doc.name,
			sales_order=doc.sales_order,
			response_payload={"reason": "outbound_mode is not Live — no HTTP call made"},
		)
		return frappe._dict(ok=False, status="Ignored", message=f"outbound_mode {settings.outbound_mode}")

	lock_name = ("mp_create:" + (doc.control_number or doc.name))[:64]
	locked = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
	if not locked or not locked[0][0]:
		return frappe._dict(ok=False, status="Ignored", message="another worker holds the lock")
	try:
		frappe.db.commit()  # refresh the REPEATABLE READ snapshot after the wait
		doc.reload()
		if doc.state not in ("Pending Create", "Creating", "Create Failed"):
			return frappe._dict(
				ok=False, status="Ignored", message=f"state changed to {doc.state} while waiting"
			)
		so = (
			frappe.db.get_value("Sales Order", doc.sales_order, ["docstatus", "status"], as_dict=True)
			if doc.sales_order
			else None
		)
		if not so or so.docstatus != 1 or so.status == "Closed":
			# never raise a bill for a stopped order; the hourly kill pass retires this row
			return frappe._dict(
				ok=False,
				status="Ignored",
				message=f"Sales Order {doc.sales_order} is cancelled or closed — no bill created",
			)

		# 1) QUERY BEFORE CREATE — adopt any payable bill already at their end.
		found = query_by_customer_reference(
			doc.control_number,
			settings,
			log_links={"malipopay_collection": doc.name, "sales_order": doc.sales_order},
		)
		for row in found.docs:
			if (row.get("status") or "").upper() in PAYABLE_STATUSES:
				_capture_provider_data(doc, row, queried_as=found.queried_as)
				doc.set_state(
					"Active",
					"Adopted existing Malipopay bill {} (query-before-create).".format(row.get("reference")),
				)
				_stamp_ml_reference_on_sales_order(doc)
				frappe.db.commit()
				notify_so_owner(
					doc.sales_order,
					"Payment number <b>{}</b> is registered with Malipopay (existing bill {}).".format(
						doc.control_number, doc.ml_reference or "-"
					),
					"green",
				)
				return frappe._dict(ok=True, status="Adopted", ml_reference=doc.ml_reference)

		# Malipopay could not be asked, so we do not know whether a bill already
		# exists. Their create is not idempotent: creating now could make a
		# second payable bill for the same number. Stop; Retry Registration
		# asks again before it ever creates.
		if not found.get("all_ok"):
			doc.set_state(
				"Create Failed",
				f"Could not ask Malipopay whether a bill exists for {doc.control_number} (see Malipopay API Log) — "
				"nothing created. Use Retry Registration when Malipopay is reachable.",
			)
			frappe.db.commit()
			notify_so_owner(
				doc.sales_order,
				f"Malipopay could not be reached for payment number <b>{doc.control_number}</b>. {_tcb_fallback(doc.control_number)} "
				"Use Retry Registration on the Malipopay Collection.",
				"orange",
			)
			return frappe._dict(
				ok=False,
				status="Failed",
				message="Malipopay could not be asked whether the bill exists — nothing created",
			)

		# 2) Write-ahead intent, then create.
		doc.set_state("Creating", "Create call in flight.")
		doc.db_set("create_attempts", cint(doc.create_attempts) + 1, update_modified=False)
		frappe.db.commit()

		payload = _build_create_payload(doc, settings)
		url = settings.base_url + CREATE_PATH
		session = get_request_session()
		http_status = None
		body = None
		try:
			response = session.post(
				url,
				json=payload,
				headers=_headers(settings),
				timeout=(settings.connect_timeout, settings.read_timeout),
				verify=bool(settings.verify_ssl),
			)
			http_status = response.status_code
			body = _parse_json_or_text(response)
		except Exception:
			create_malipopay_api_log(
				direction="Outbound",
				event_type="Collection Create",
				status="Failed",
				processing_mode="Live",
				endpoint=url,
				control_number=doc.control_number,
				malipopay_collection=doc.name,
				sales_order=doc.sales_order,
				request_payload=payload,
				error=frappe.get_traceback(),
			)
			doc.set_state(
				"Create Failed", "HTTP call failed/timed out — repair job must QUERY before any retry."
			)
			frappe.db.commit()
			notify_so_owner(
				doc.sales_order,
				f"Malipopay did not answer while registering payment number <b>{doc.control_number}</b>. {_tcb_fallback(doc.control_number)} "
				"Use Retry Registration on the Malipopay Collection.",
				"orange",
			)
			return frappe._dict(ok=False, status="Failed", message="HTTP error — see Malipopay API Log")

		success = bool(isinstance(body, dict) and body.get("success"))
		returned_ref = cstr(
			((body.get("data") or {}) if isinstance(body, dict) else {}).get("reference") or ""
		).strip()
		no_reference = success and not returned_ref
		if no_reference:
			# Without their reference the bill cannot be read, paid by mobile
			# money or matched — treat as failed. Retry asks before creating,
			# so a bill that did get made is adopted, never duplicated.
			success = False
		create_malipopay_api_log(
			direction="Outbound",
			event_type="Collection Create",
			status="Success" if success else "Failed",
			processing_mode="Live",
			endpoint=url,
			http_status_code=http_status,
			control_number=doc.control_number,
			ml_reference=((body.get("data") or {}).get("reference") if isinstance(body, dict) else None),
			malipopay_collection=doc.name,
			sales_order=doc.sales_order,
			request_payload=payload,
			response_payload=body,
		)

		if success:
			# The create response omits customerReference (proven ML228501,
			# 2026-09-08) — but storage is verbatim-as-sent, so the reference
			# WE sent IS the stored customerReference.
			_capture_provider_data(doc, body.get("data") or {}, queried_as=payload.get("reference"))
			doc.set_state("Active", "Collection created at Malipopay (code {}).".format(body.get("code")))
			_stamp_ml_reference_on_sales_order(doc)
			frappe.db.commit()
			notify_so_owner(
				doc.sales_order,
				"Payment number <b>{}</b> registered successfully with Malipopay (bill {}).".format(
					doc.control_number, doc.ml_reference or "-"
				),
				"green",
			)
			return frappe._dict(ok=True, status="Created", ml_reference=doc.ml_reference)

		message = ""
		if no_reference:
			message = "Malipopay answered success but gave no reference back"
		elif isinstance(body, dict):
			message = str(body.get("message") or body.get("error") or body.get("metadata") or "")[:300]
		doc.set_state("Create Failed", f"Malipopay refused: {message or http_status}")
		frappe.db.commit()
		notify_so_owner(
			doc.sales_order,
			f"Malipopay could not register payment number <b>{doc.control_number}</b>: {message or http_status}. {_tcb_fallback(doc.control_number)} "
			"Use Retry Registration on the Malipopay Collection.",
			"red",
		)
		return frappe._dict(ok=False, status="Failed", message=message)
	finally:
		try:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
		except Exception:
			pass


def _capture_provider_data(doc, data, *, queried_as=None):
	"""Capture the response onto the Collection row (the operator-visible
	mirror; the full payload lives in Malipopay API Log)."""
	if data.get("reference"):
		doc.ml_reference = data.get("reference")
	stored_ref = data.get("customerReference") or queried_as
	if stored_ref:
		doc.stored_customer_reference = stored_ref
	if data.get("amount") is not None:
		doc.amount = cint(data.get("amount"))
	if data.get("status"):
		doc.provider_status = data.get("status")
	if data.get("paidAmount") is not None:
		doc.provider_paid_amount = flt(data.get("paidAmount"))
	if data.get("link"):
		doc.payment_link = data.get("link")
	doc.last_synced_at = now()


WEBHOOK_REGISTER_PATH = "/api/v1/project/webhook"
WEBHOOK_ENDPOINT_PATH = "/api/method/landms.api.malipopay.webhook"
WEBHOOK_EVENTS = ["payment.confirmed", "payment.failed"]


@frappe.whitelist(methods=["POST"])  # changes data / calls Malipopay or TCB
def register_webhook(url=None, dry_run=0):
	"""FALLBACK ONLY — the normal way is the Malipopay Merchant dashboard
	(Settings → Developer Tools → Webhooks → Add Webhook, event PAYMENT_UPDATE),
	which shows the secret to paste into Malipopay Settings. This API route
	(QRG §3) exists for the day the dashboard cannot be used. One webhook per
	MERCHANT project serves every Land Acquisition. The reply's signing secret
	is stored straight into the Password field and never returned or logged.
	dry_run=1 returns the exact request without sending anything."""
	frappe.only_for("System Manager")
	settings = get_malipopay_settings()
	if not settings.enabled or not settings.base_url or not settings.api_token:
		frappe.throw(
			_(
				"Malipopay Settings must be enabled with Base URL and API Token before registering the webhook."
			)
		)
	url = (url or "").strip() or (frappe.utils.get_url() + WEBHOOK_ENDPOINT_PATH)
	if not url.startswith("https://") and not cint(dry_run):
		frappe.throw(_("The callback URL must be the live site's https address, got {0}").format(url))
	if not url.endswith(WEBHOOK_ENDPOINT_PATH):
		frappe.throw(_("The callback URL must end with {0}").format(WEBHOOK_ENDPOINT_PATH))
	settings_doc = frappe.get_doc("Malipopay Settings")
	if settings_doc.get_password("webhook_signing_secret", raise_exception=False) and not cint(dry_run):
		frappe.throw(
			_(
				"A webhook signing secret is already stored — the project is already registered. "
				"Re-registration semantics are not confirmed with Lockwood; clear the secret deliberately first if you really mean to redo it."
			)
		)

	payload = {"url": url, "service": "Collection", "events": list(WEBHOOK_EVENTS)}
	endpoint = settings.base_url + WEBHOOK_REGISTER_PATH
	if cint(dry_run):
		return {
			"ok": True,
			"dry_run": True,
			"endpoint": endpoint,
			"payload": payload,
			"message": "Dry run — nothing sent. This exact request would be POSTed with the apiToken header.",
		}

	session = get_request_session()
	http_status = None
	body = None
	try:
		response = session.post(
			endpoint,
			json=payload,
			headers=_headers(settings),
			timeout=(settings.connect_timeout, settings.read_timeout),
			verify=bool(settings.verify_ssl),
		)
		http_status = response.status_code
		body = _parse_json_or_text(response)
	except Exception:
		create_malipopay_api_log(
			direction="Outbound",
			event_type="Webhook",
			status="Failed",
			processing_mode="Register",
			endpoint=endpoint,
			request_payload=payload,
			error=frappe.get_traceback(),
		)
		frappe.db.commit()
		return {"ok": False, "message": "Registration call failed before a reply — see Malipopay API Log."}

	secret = _find_secret(body)
	create_malipopay_api_log(
		direction="Outbound",
		event_type="Webhook",
		status="Success" if (secret and http_status == 200) else "Failed",
		processing_mode="Register",
		endpoint=endpoint,
		http_status_code=http_status,
		request_payload=payload,
		response_payload=_redact(body),
		error=None if secret else "no signing secret found in the registration reply",
	)
	if not secret:
		frappe.db.commit()
		return {
			"ok": False,
			"http_status": http_status,
			"message": f"Malipopay answered HTTP {http_status} but no signing secret was found in the reply — nothing stored; see Malipopay API Log (reply redacted).",
		}

	settings_doc.webhook_signing_secret = secret
	settings_doc.flags.ignore_permissions = True
	settings_doc.save()
	frappe.db.commit()
	return {
		"ok": True,
		"http_status": http_status,
		"registered_url": url,
		"message": f"Registered {url}. Signing secret stored in Malipopay Settings (not displayed). Inbound Mode stays as configured — start with Log Only.",
	}


_SECRET_KEYS = (
	"secret",
	"webhooksecret",
	"signingsecret",
	"webhook_secret",
	"signing_secret",
	"secretkey",
	"secret_key",
)


def _find_secret(body):
	"""Their reply shape is not documented beyond 'carries the secret' — look for
	any secret-ish key at any depth, first match wins."""
	if isinstance(body, dict):
		for key, value in body.items():
			if str(key).replace("-", "").lower() in _SECRET_KEYS and isinstance(value, str) and value.strip():
				return value.strip()
		for value in body.values():
			found = _find_secret(value)
			if found:
				return found
	elif isinstance(body, list):
		for item in body:
			found = _find_secret(item)
			if found:
				return found
	return None


def _redact(body):
	"""Copy of the reply with every secret-ish value masked — safe for the API log."""
	if isinstance(body, dict):
		return {
			k: ("***" if str(k).replace("-", "").lower() in _SECRET_KEYS else _redact(v))
			for k, v in body.items()
		}
	if isinstance(body, list):
		return [_redact(v) for v in body]
	return body


def notify_so_owner(sales_order_name, message, indicator="green"):
	"""Pop the same kind of confirmation TCB shows on registration.

	TCB registers inline during submit, so its message appears in the submit
	dialog (tcb.py::_notify_so_owner / msgprint). Malipopay's bill is created by
	a background worker AFTER the sale commits, so the popup must be pushed to
	the operator's screen when the bill actually exists — never a promise made
	before the fact. Best-effort: a failed notification must not affect the bill.
	"""
	try:
		user = frappe.db.get_value("Sales Order", sales_order_name, "owner") if sales_order_name else None
		if not user:
			return
		frappe.publish_realtime(
			"msgprint", {"message": message, "alert": True, "indicator": indicator}, user=user
		)
	except Exception:
		pass


def _stamp_ml_reference_on_sales_order(doc):
	"""Display mirror only (QRG: 'store this on the Sales Order') — the
	Malipopay Collection register stays the authority. Best-effort: never
	breaks the money path, and survives a deploy where this code lands
	before the after_migrate custom-field sync has created the column."""
	try:
		if (
			doc.ml_reference
			and doc.sales_order
			and frappe.db.has_column("Sales Order", "malipopay_reference")
		):
			frappe.db.set_value(
				"Sales Order", doc.sales_order, "malipopay_reference", doc.ml_reference, update_modified=False
			)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"Malipopay SO mirror stamp failed (money path unaffected): {doc.name}",
		)


def _build_create_payload(doc, settings):
	"""The create payload in the shape Lockwood supplied on 2026-10-01:
	service "mobile", no provider (Malipopay works out the network from the
	phone number). Bills raised with service "account" + provider "CRDB BANK"
	refused payment (code 91050, 2026-09-29). `push` is the boolean of
	contract v1.2 §5 — false, so no prompt reaches the customer's phone.
	Integer TZS and a one-line description as before; amountType stays the
	Malipopay Settings choice."""
	so = (
		frappe.db.get_value(
			"Sales Order",
			doc.sales_order,
			["name", "customer_name", "grand_total", "rounded_total", "contact_mobile", "plot"],
			as_dict=True,
		)
		or frappe._dict()
	)
	plot_number = ""
	if so.get("plot"):
		plot_number = frappe.db.get_value("Plot Master", so.plot, "plot_number") or ""
		plot_number = plot_number.split(" PLAN")[0].split(" (PLAN")[0]
	amount = cint(doc.amount) or cint(round(flt(so.get("rounded_total") or so.get("grand_total"))))
	description = "Kumb. Na. {} | Ndg: {}".format(
		so.get("name") or doc.sales_order, (so.get("customer_name") or "").strip() or "-"
	)
	if plot_number:
		description += f" | {plot_number}"
	description = " ".join(description.split())  # hard one-line guarantee
	account = (so.get("contact_mobile") or "").strip() or "255000000000"
	return {
		"reference": doc.control_number,
		"description": description[:200],
		"amount": amount,
		"service": "mobile",
		"account": account,
		"amountType": settings.amount_type or "FLEXIBLE",
		"push": False,
	}


# ---------------------------------------------------------------------------
# Sales Order wiring — the dependent-rail hooks
# ---------------------------------------------------------------------------
# TCB is the ISSUER: it gets a register-first gate that may BLOCK a submit.
# Malipopay is a DEPENDENT rail: its bill must never block, break, or poison
# a sale. So: the Collection row is inserted INSIDE the SO transaction (it
# rolls back if the submit fails), and the non-idempotent HTTP create runs in
# a background worker only AFTER the sale has committed (enqueue_after_commit)
# — never inline, per the mid-transaction-commit scar in sales_order_hooks.


def on_sales_order_submit(doc, method=None):
	"""doc_events: Sales Order on_submit. Never throws, never commits, never
	calls HTTP inline. Guard ladder (all must pass, else silently stand down):
	plot sales order -> control number minted -> LA has Collect via Malipopay
	ticked -> Malipopay Settings enabled and outbound_mode Log Only/Live ->
	no live collection epoch already on this control number."""
	try:
		_stage_collection_for_sales_order(doc)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"Malipopay submit wiring failed (sale unaffected): {doc.name}",
		)


def collects_via_malipopay(sales_order):
	"""The one definition of an order the Malipopay rail may act on: a plot
	sale whose project (Land Acquisition) has Collect via Malipopay ticked.
	Trading and other Sales Orders are never touched. Takes a Sales Order
	name or the document itself."""
	from landms.sales_order_hooks import _is_landms_sales_order

	so = sales_order
	if isinstance(sales_order, str):
		so = frappe.db.get_value(
			"Sales Order", sales_order, ["plot", "plot_application", "land_acquisition"], as_dict=True
		)
	if not so or not _is_landms_sales_order(so) or not so.get("land_acquisition"):
		return False
	if not frappe.db.has_column("Land Acquisition", "collect_via_malipopay"):
		return False
	return bool(
		cint(frappe.db.get_value("Land Acquisition", so.get("land_acquisition"), "collect_via_malipopay"))
	)


def can_take_payment_now(sales_order_name, control_number):
	"""payments.router's question when the issuer (TCB) could not register a
	number at submit: can Malipopay take payment on it instead? True when the
	order collects via Malipopay, the rail is Live, and Malipopay answers a
	lookup of the number — a read only: nothing is created and nothing is
	committed. The bill itself is created right after the submit, as for every
	order. Never throws."""
	try:
		if not control_number or not collects_via_malipopay(sales_order_name):
			return False
		settings = get_malipopay_settings()
		if not settings.enabled or settings.outbound_mode != "Live" or not settings.api_token:
			return False
		if settings.inbound_mode != "Apply Payment":
			return False  # its payments would not be recorded, and the order would look unpaid
		# the lookup logs the number itself; only the order is passed as a link
		found = query_by_customer_reference(
			control_number, settings, log_links={"sales_order": sales_order_name}
		)
		return bool(found.get("all_ok"))
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"Malipopay availability check failed (treated as unavailable): {sales_order_name}",
		)
		return False


def _sold_with_malipopay(so):
	"""True when the order got a Malipopay bill when it was sold.

	Operator, 15 Oct 2026: an order sold before Malipopay was switched on for
	its project stays with TCB for good; a Reopen or an amendment never
	brings it in. Every order sold with Malipopay on gets its bill row inside
	its own submit, so any bill row, in any state, on the order, on the
	orders it was amended from, or on its payment number, is the test."""
	names = [so.name]
	amended_from = so.get("amended_from")
	while amended_from and amended_from not in names and len(names) < 20:
		names.append(amended_from)
		amended_from = frappe.db.get_value("Sales Order", amended_from, "amended_from")
	if frappe.db.exists("Malipopay Collection", {"sales_order": ["in", names]}):
		return True
	number = cstr(so.get("control_number")).strip()
	return bool(number and frappe.db.exists("Malipopay Collection", {"control_number": number}))


def _stage_collection_for_sales_order(doc, amount=None):
	"""amount: what the bill asks for; None = the whole order (a new sale)."""
	if not collects_via_malipopay(doc):
		return  # not a plot sale on a Malipopay project
	if not doc.get("control_number"):
		return  # no minted number -> nothing to bill
	if (amount is not None or doc.get("amended_from")) and not _sold_with_malipopay(doc):
		return  # reopened or amended order from before Malipopay: it stays with TCB
	settings = get_malipopay_settings()
	if not settings.enabled or settings.outbound_mode not in ("Log Only", "Live"):
		return

	# One live epoch per control number: an amended/resubmitted order whose
	# number already carries a live collection stages nothing new.
	if frappe.db.get_all(
		"Malipopay Collection",
		filters={"control_number": doc.control_number, "state": ("!=", "Dead")},
		limit=1,
	):
		return

	mpc = frappe.get_doc(
		{
			"doctype": "Malipopay Collection",
			"control_number": doc.control_number,
			"sales_order": doc.name,
			"land_acquisition": doc.get("land_acquisition"),
			"customer": doc.get("customer"),
			"amount": cint(amount) if amount else None,
		}
	)
	mpc.insert(ignore_permissions=True)  # inside the SO transaction on purpose

	if settings.outbound_mode == "Log Only":
		create_malipopay_api_log(
			direction="Outbound",
			event_type="Collection Create",
			status="Ignored",
			processing_mode="Log Only",
			control_number=doc.control_number,
			sales_order=doc.name,
			malipopay_collection=mpc.name,
			request_payload=_build_create_payload(mpc, settings),
			error="Log Only mode — staged Pending Create, no call made.",
		)
		return

	frappe.enqueue(
		"landms.malipopay.create_or_adopt_collection",
		collection_name=mpc.name,
		queue="short",
		enqueue_after_commit=True,
	)


def on_sales_order_cancel(doc, method=None):
	"""doc_events: Sales Order on_cancel. A cancelled sale must never leave a
	payable bill. The decline in before_cancel has normally asked for the kill
	already; this repeats it (harmless) for any cancel that reached here
	another way. Never throws."""
	if doc.get("control_number"):
		request_kill_for_sales_order(doc.name, doc.control_number, f"Sales Order {doc.name} cancelled")


# ---------------------------------------------------------------------------
# Stop — tell Malipopay when LandMS stops a number (contract v1.2 §12.2)
# ---------------------------------------------------------------------------
# Every place that declines a number at TCB (cancel, close, the nightly
# reaper, contract termination, the TCB decline button) goes through
# payments.router.decline_reference_for_sales_order, which calls
# follow_decline_for_sales_order once TCB has declined. What the bill gets
# depends on whether the order holds money:
#   * money on it — a CLOSED, forfeited order that may be reopened: SUSPENDED.
#     Nobody can pay, the money stays, and Reopen lifts the pause (PROCESSING);
#   * no money — a CANCELLED order: EXPIRED, final at Malipopay;
#   * a TERMINATED contract (button or nightly auto-termination) — EXPIRED even
#     with money on it: a termination never reopens (operator rule, 14 Oct 2026).
# A closed order is later either reopened (resume) or closed for good by a
# System Manager (expire; LandMS then refuses to reopen it).
# Only the intent ("… Requested") is written inside the caller's transaction;
# the HTTP call runs in a worker AFTER the commit, so a stop that rolls back
# never touches the bill. kill_pending_collections (hourly) retries anything
# that did not land and catches orders stopped some other way.

STATUS_PATH = "/api/v1/payment/{0}/status"
# Their statuses on which a bill can no longer take money.
CLOSED_STATUSES = ("EXPIRED", "CANCELLED", "SUCCESSFUL", "PAID")
KILL_FLAG = "Bill not yet expired at Malipopay"
SUSPEND_FLAG = "Bill not yet suspended at Malipopay"
RESUME_FLAG = "Bill not yet reopened at Malipopay"
# §12.2 refusals: the move is not allowed from the bill's status (an expired
# bill is final), and the bill is already paid — in full, or by the TCB
# payments we report to them (section 11).
CODE_MOVE_NOT_ALLOWED = 3173
CODE_ALREADY_PAID = 3174
# The Sales Order flag set by "Close for good at Malipopay"; Reopen refuses it.
CLOSED_FOR_GOOD_FIELD = "malipopay_closed_for_good"
_MOVES = {
	# status sent: (log event_type, flag raised while it has not landed, word once landed)
	"EXPIRED": ("Expire", KILL_FLAG, "expired"),
	"SUSPENDED": ("Suspend", SUSPEND_FLAG, "suspended"),
	"PROCESSING": ("Resume", RESUME_FLAG, "reopened"),
}


def follow_decline_for_sales_order(sales_order_name, control_number, reason, final=False):
	"""payments.router's Malipopay follower, called once TCB has declined the
	number: pause the bill of an order that holds money (it may be reopened),
	expire it otherwise — and always when the stop is final (a contract
	termination, which can never be reopened). No HTTP, no commit, never throws."""
	try:
		control_number = cstr(control_number).strip()
		if not control_number:
			return
		live = frappe.db.get_all(
			"Malipopay Collection",
			filters={"control_number": control_number, "state": ("!=", "Dead")},
			pluck="name",
		)
		if not live:
			return  # no Malipopay bill on this number (every TCB-only project): nothing to do
		pause = not final and _order_holds_money(sales_order_name) and not _stopped_for_good(sales_order_name)
		for name in live:
			mpc = frappe.get_doc("Malipopay Collection", name)
			if pause:
				request_suspend_for_collection(mpc, reason)
			else:
				request_kill_for_collection(mpc, reason)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"Malipopay stop request failed (stop unaffected): {sales_order_name}",
		)


def _order_holds_money(sales_order_name):
	"""True when the customer has paid something on this order — the line
	between a close (money kept, may be reopened) and a cancel. A Closed order
	is judged by its forfeiture entry, which exists only when money was paid
	(Reopen needs it too); before that, by what its plot invoice has received —
	the test the nightly reaper uses to choose close over cancel."""
	if not sales_order_name:
		return False
	so = frappe.db.get_value(
		"Sales Order",
		sales_order_name,
		["docstatus", "status", "forfeiture_entry", "plot_sales_invoice"],
		as_dict=True,
	)
	if not so or so.docstatus != 1:
		return False
	if so.status == "Closed":
		return bool(so.forfeiture_entry)
	if not so.plot_sales_invoice:
		return False
	invoice = frappe.db.get_value(
		"Sales Invoice",
		so.plot_sales_invoice,
		["docstatus", "grand_total", "outstanding_amount"],
		as_dict=True,
	)
	return bool(
		invoice and invoice.docstatus == 1 and flt(invoice.grand_total) - flt(invoice.outstanding_amount) > 0
	)


def is_closed_for_good(sales_order_name):
	"""True once "Close for good at Malipopay" was used on this order."""
	if not sales_order_name or not frappe.db.has_column("Sales Order", CLOSED_FOR_GOOD_FIELD):
		return False
	return bool(cint(frappe.db.get_value("Sales Order", sales_order_name, CLOSED_FOR_GOOD_FIELD)))


def before_change_sales_order(doc, method=None):
	"""doc_events: Sales Order before_change — every db_set runs it, and
	ERPNext's Status > Re-open sets the status that way. An order closed for
	good at Malipopay is final, so lifting its Closed status is refused there
	too (LandMS's own Reopen already refuses it)."""
	if doc.docstatus != 1 or doc.get("status") == "Closed" or not doc.get("plot"):
		return
	if frappe.db.get_value("Sales Order", doc.name, "status") == "Closed" and is_closed_for_good(doc.name):
		frappe.throw(
			_(
				"Sales Order {0} was closed for good at Malipopay and can no longer be reopened. "
				"A returning customer needs a new order."
			).format(doc.name)
		)


def _contract_terminated(sales_order_name):
	"""True when the order's plot contract was terminated — the customer failed
	for good and the order can never be reopened (operator rule, 14 Oct 2026)."""
	return bool(
		sales_order_name
		and frappe.db.exists(
			"Plot Contract",
			{"sales_order": sales_order_name, "contract_status": "Terminated", "docstatus": 1},
		)
	)


def _stopped_for_good(sales_order_name):
	"""True when nothing can reopen this order: closed for good, or terminated."""
	return is_closed_for_good(sales_order_name) or _contract_terminated(sales_order_name)


def request_kill_for_sales_order(sales_order_name, control_number, reason):
	"""Mark every live bill on this number for killing and queue the expire
	call. No HTTP, no commit, never throws — the stop it rides on must not
	depend on Malipopay."""
	try:
		control_number = cstr(control_number).strip()
		if not control_number:
			return
		for name in frappe.db.get_all(
			"Malipopay Collection",
			filters={"control_number": control_number, "state": ("!=", "Dead")},
			pluck="name",
		):
			request_kill_for_collection(frappe.get_doc("Malipopay Collection", name), reason)
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			f"Malipopay kill request failed (stop unaffected): {sales_order_name}",
		)


def request_kill_for_collection(mpc, reason, enqueue=True):
	"""Mark one bill for killing. A bill that never reached Malipopay simply
	dies; one that may exist there goes to Kill Requested and, with enqueue,
	its expire call is queued for after the caller's commit. No HTTP."""
	if mpc.state == "Creating" and get_datetime(mpc.modified) > add_to_date(now_datetime(), minutes=-30):
		# a create call may still be in flight — the hourly pass kills it once it settles
		mpc.flag_attention(
			f"{reason} while registering with Malipopay — it will be expired once registration settles."
		)
		return
	if (
		not mpc.ml_reference
		and not cint(mpc.create_attempts)
		and mpc.state in ("Pending Create", "Create Failed")
	):
		mpc.set_state("Dead", f"{reason} — no bill was ever sent to Malipopay.")
		return
	if mpc.state != "Kill Requested":
		mpc.set_state("Kill Requested", f"{reason} — bill to be expired at Malipopay.")
	queued = frappe.flags.setdefault("mp_kill_queued", set())
	if enqueue and mpc.name not in queued:
		queued.add(mpc.name)
		frappe.enqueue(
			"landms.malipopay.expire_collection",
			collection_name=mpc.name,
			queue="short",
			job_id="mp_kill_" + mpc.name,
			deduplicate=True,
			enqueue_after_commit=True,
		)


def request_suspend_for_collection(mpc, reason, enqueue=True):
	"""Mark one bill to be paused at Malipopay (its order was closed with money
	on it). A bill that never got a Malipopay reference has nothing to pause
	there, so it is stopped like any other; Reopen then raises a new one for
	what is still owed. No HTTP."""
	if mpc.state in ("Suspended", "Kill Requested", "Dead"):
		return
	if mpc.state != "Suspend Requested":
		if not mpc.ml_reference or mpc.state not in ("Active", "Needs Attention", "Resume Requested"):
			request_kill_for_collection(mpc, reason, enqueue=enqueue)
			return
		mpc.set_state("Suspend Requested", f"{reason} — bill to be suspended at Malipopay.")
	if enqueue:
		_enqueue_move(mpc, "suspend_collection")


def request_resume_for_collection(mpc, reason, enqueue=True):
	"""Mark a paused bill to be reopened at Malipopay (its order was reopened).
	Sending PROCESSING to a bill that is already open is harmless (they answer
	changed: false), so a suspend that may or may not have landed is resumed
	the same way. No HTTP."""
	if mpc.state not in ("Suspend Requested", "Suspended", "Resume Requested"):
		return
	if mpc.state != "Resume Requested":
		mpc.set_state("Resume Requested", f"{reason} — bill to be reopened at Malipopay.")
	if enqueue:
		_enqueue_move(mpc, "resume_collection")


def _enqueue_move(mpc, method):
	"""Queue a suspend/resume worker for after the caller's commit, once per
	request. The worker re-checks the state, so a stale job does nothing."""
	queued = frappe.flags.setdefault("mp_move_queued", set())
	key = method + ":" + mpc.name
	if key in queued:
		return
	queued.add(key)
	frappe.enqueue(
		"landms.malipopay." + method,
		collection_name=mpc.name,
		queue="short",
		job_id="mp_" + method + "_" + mpc.name,
		deduplicate=True,
		enqueue_after_commit=True,
	)


def _kill_reason(doc):
	"""The reason Malipopay records on the expired bill."""
	so = (
		frappe.db.get_value("Sales Order", doc.sales_order, ["docstatus", "status"], as_dict=True)
		if doc.sales_order
		else None
	)
	if so and so.docstatus == 2:
		return f"Sales Order {doc.sales_order} cancelled in LandMS"
	if so and so.status == "Closed" and _contract_terminated(doc.sales_order):
		return f"Contract of Sales Order {doc.sales_order} terminated in LandMS"
	if so and so.status == "Closed" and is_closed_for_good(doc.sales_order):
		return f"Sales Order {doc.sales_order} closed for good in LandMS"
	if so and so.status == "Closed":
		return f"Sales Order {doc.sales_order} closed in LandMS"
	return f"Payment number {doc.control_number} declined in LandMS"


def _move_reason(doc, status):
	"""The reason Malipopay records with a suspend or resume."""
	if status == "SUSPENDED":
		return f"Sales Order {doc.sales_order or doc.control_number} closed in LandMS; payments paused until it is reopened or closed for good"
	return f"Sales Order {doc.sales_order or doc.control_number} reopened in LandMS"


def _provider_status(settings, doc):
	"""Their current status for this bill, or None when it cannot be read."""
	found = query_by_customer_reference(
		doc.control_number,
		settings,
		log_links={"malipopay_collection": doc.name, "sales_order": doc.sales_order},
	)
	for row in found.docs:
		if not doc.ml_reference or row.get("reference") == doc.ml_reference:
			return cstr(row.get("status")).upper() or None
	return None


def expire_collection(collection_name):
	"""Worker: expire one Kill Requested bill at Malipopay. Owns its commits,
	never raises. Expired (or already closed at their end) -> Dead; anything
	else stays Kill Requested and flagged, and the hourly pass tries again."""
	settings = get_malipopay_settings()
	doc = frappe.get_doc("Malipopay Collection", collection_name)
	if doc.state != "Kill Requested":
		return frappe._dict(ok=False, status="Ignored", message=f"state is {doc.state}")
	if not settings.enabled or not settings.api_token or settings.outbound_mode != "Live":
		doc.flag_attention(f"{KILL_FLAG}: Malipopay outbound is not Live, so the expire call was not made.")
		frappe.db.commit()
		return frappe._dict(ok=False, status="Ignored", message=f"outbound_mode {settings.outbound_mode}")

	lock_name = ("mp_kill:" + doc.name)[:64]
	locked = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
	if not locked or not locked[0][0]:
		return frappe._dict(ok=False, status="Ignored", message="another worker holds the lock")
	try:
		frappe.db.commit()  # fresh snapshot after the wait
		doc.reload()
		if doc.state != "Kill Requested":
			return frappe._dict(
				ok=False, status="Ignored", message=f"state changed to {doc.state} while waiting"
			)
		if not doc.ml_reference:
			settled = _find_bill_to_expire(doc, settings)
			if settled:
				return settled
		sent = _send_status(doc, settings, "EXPIRED", _kill_reason(doc))
		if sent.ok:
			_mark_killed(
				doc, "EXPIRED", "Expired at Malipopay ({}).".format(sent.data.get("expiredAt") or now())
			)
			return frappe._dict(ok=True, status="Dead", message="expired")
		return _settle_refused_move(doc, settings, sent, "EXPIRED")
	finally:
		_release_lock(lock_name)


def suspend_collection(collection_name):
	"""Worker: pause one Suspend Requested bill at Malipopay (its order was
	closed with money on it). Owns its commits, never raises."""
	return _move_collection(collection_name, "Suspend Requested", "SUSPENDED")


def resume_collection(collection_name):
	"""Worker: lift the pause on one Resume Requested bill (its order was
	reopened). Owns its commits, never raises."""
	return _move_collection(collection_name, "Resume Requested", "PROCESSING")


def _move_collection(collection_name, requested_state, status):
	"""Send one suspend/resume move under the bill's lock (the lock the expire
	takes too, so two moves on one bill never cross). Landed -> Suspended or
	Active; refused or unanswered -> settled from their status where possible,
	else left as requested and flagged for the hourly retry."""
	event, flag, done = _MOVES[status]
	settings = get_malipopay_settings()
	doc = frappe.get_doc("Malipopay Collection", collection_name)
	if doc.state != requested_state:
		return frappe._dict(ok=False, status="Ignored", message=f"state is {doc.state}")
	if not settings.enabled or not settings.api_token or settings.outbound_mode != "Live":
		doc.flag_attention(
			f"{flag}: Malipopay outbound is not Live, so the {event.lower()} call was not made."
		)
		frappe.db.commit()
		return frappe._dict(ok=False, status="Ignored", message=f"outbound_mode {settings.outbound_mode}")

	lock_name = ("mp_kill:" + doc.name)[:64]
	locked = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
	if not locked or not locked[0][0]:
		return frappe._dict(ok=False, status="Ignored", message="another worker holds the lock")
	try:
		frappe.db.commit()  # fresh snapshot after the wait
		doc.reload()
		if doc.state != requested_state:
			return frappe._dict(
				ok=False, status="Ignored", message=f"state changed to {doc.state} while waiting"
			)
		sent = _send_status(doc, settings, status, _move_reason(doc, status))
		if sent.ok:
			_land_move(doc, status, cstr(sent.data.get("status")).upper() or status)
			return frappe._dict(ok=True, status=doc.state, message=done)
		return _settle_refused_move(doc, settings, sent, status)
	finally:
		_release_lock(lock_name)


def _land_move(doc, status, provider_status):
	"""Record a suspend or resume that Malipopay holds."""
	flag = _MOVES[status][1]
	doc.provider_status = provider_status
	if cstr(doc.attention_reason).startswith(flag):
		doc.needs_attention = 0
		doc.attention_reason = ""
	if status == "SUSPENDED":
		doc.set_state(
			"Suspended",
			"Suspended at Malipopay — nobody can pay until the order is reopened or closed for good.",
		)
	else:
		doc.set_state("Active", f"Reopened at Malipopay ({provider_status}).")
	frappe.db.commit()


def _release_lock(lock_name):
	try:
		frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
	except Exception:
		pass


def _kill_log_links(doc):
	return {"malipopay_collection": doc.name, "sales_order": doc.sales_order}


def _find_bill_to_expire(doc, settings):
	"""A bill with no Malipopay reference: a create that timed out may still
	have made one, so ask before deciding. Returns None when an open bill was
	found and adopted (go on and expire it), else the final result."""
	found = query_by_customer_reference(doc.control_number, settings, log_links=_kill_log_links(doc))
	live = [r for r in found.docs if cstr(r.get("status")).upper() in PAYABLE_STATUSES + ("SUSPENDED",)]
	if live:
		_capture_provider_data(doc, live[0], queried_as=found.queried_as)
		doc.save(ignore_permissions=True)
		frappe.db.commit()
		return None
	if found.get("all_ok"):
		doc.set_state(
			"Dead", f"Malipopay holds no payable bill for {doc.control_number} — nothing to expire."
		)
		frappe.db.commit()
		return frappe._dict(ok=True, status="Dead", message="no payable bill at Malipopay")
	doc.flag_attention(
		f"{KILL_FLAG}: could not ask Malipopay whether a bill exists for {doc.control_number}; retried hourly."
	)
	frappe.db.commit()
	return frappe._dict(ok=False, status="Failed", message="Malipopay could not be asked")


def _send_status(doc, settings, status, reason):
	"""POST one lifecycle move to /api/v1/payment/{ML}/status (contract v1.2
	§12.2) and log it. Returns _dict(ok, data, body, http_status, code); never
	raises. ok means Malipopay now holds the bill in the asked state — a repeat
	of the state it is already in counts (they answer changed: false)."""
	event = _MOVES[status][0]
	url = settings.base_url + STATUS_PATH.format(doc.ml_reference)
	payload = {
		"status": status,
		"reason": cstr(reason).strip()[:500] or "Changed in LandMS",
		"externalReference": cstr(doc.sales_order or doc.name)[:120],
	}
	http_status = body = error = None
	try:
		response = get_request_session().post(
			url,
			json=payload,
			headers=_headers(settings),
			timeout=(settings.connect_timeout, settings.read_timeout),
			verify=bool(settings.verify_ssl),
		)
		http_status = response.status_code
		body = _parse_json_or_text(response)
	except Exception:
		error = frappe.get_traceback()
	data = (body.get("data") or {}) if isinstance(body, dict) else {}
	landed = cstr(data.get("status")).upper()
	wanted = ("PROCESSING", "PARTIAL") if status == "PROCESSING" else (status,)
	ok = bool(isinstance(body, dict) and body.get("success") and landed in wanted)
	create_malipopay_api_log(
		direction="Outbound",
		event_type=event,
		status="Success" if ok else "Failed",
		processing_mode="Live",
		endpoint=url,
		http_status_code=http_status,
		control_number=doc.control_number,
		ml_reference=doc.ml_reference,
		request_payload=payload,
		response_payload=body,
		error=error,
		**_kill_log_links(doc),
	)
	return frappe._dict(
		ok=ok,
		data=data,
		body=body,
		http_status=http_status,
		code=cint(body.get("code")) if isinstance(body, dict) else 0,
	)


def _settle_refused_move(doc, settings, sent, status):
	"""A move was refused or not answered. Their own status for the bill
	settles it where it can; otherwise the bill stays as requested, flagged,
	and the hourly pass sends the move again."""
	event, flag, done = _MOVES[status]
	theirs = _provider_status(settings, doc)
	if theirs in ("EXPIRED", "CANCELLED"):
		_mark_killed(doc, theirs, f"Malipopay already shows the bill {theirs}.")
		if status == "PROCESSING":
			# Closed at their end, not by our Close for good: the reopened order
			# still needs a way to pay, so raise a new bill for what is owed.
			_replace_closed_bill(doc)
			return frappe._dict(
				ok=False,
				status="Dead",
				message=f"already {theirs} at Malipopay; a new bill is raised",
			)
		return frappe._dict(ok=True, status="Dead", message="already " + theirs)
	if theirs in ("SUCCESSFUL", "PAID"):
		_mark_killed(doc, theirs, f"Bill was paid in full at Malipopay — nothing left to {event.lower()}.")
		if status != "PROCESSING":
			doc.flag_attention(
				f"Bill {doc.ml_reference} was paid in full at Malipopay after LandMS stopped the order — "
				"check the money in Payment Receipt Ledger."
			)
			frappe.db.commit()
		return frappe._dict(ok=True, status="Dead", message="paid in full at Malipopay")
	if sent.code == CODE_ALREADY_PAID:
		_mark_killed(
			doc,
			theirs or doc.provider_status,
			f"Malipopay refused the {event.lower()}: the bill is already covered (code {CODE_ALREADY_PAID}), so it can take no more money.",
		)
		return frappe._dict(ok=True, status="Dead", message="already covered")
	if status == "SUSPENDED" and theirs == "SUSPENDED":
		_land_move(doc, status, theirs)
		return frappe._dict(ok=True, status=doc.state, message="already suspended")
	if status == "PROCESSING" and theirs in ("PROCESSING", "PARTIAL"):
		_land_move(doc, status, theirs)
		return frappe._dict(ok=True, status=doc.state, message="already open")

	message = (
		cstr(sent.body.get("message") or sent.body.get("error") or "")[:200]
		if isinstance(sent.body, dict)
		else "no answer"
	)
	doc.db_set(
		"last_event",
		"{} attempt failed ({}): {}".format(event, sent.http_status or "no answer", message)[:500],
		update_modified=False,
	)
	if status == "PROCESSING":
		advice = "The customer cannot pay through Malipopay yet ({}); retried hourly.".format(
			"TCB still works"
			if _tcb_takes_payment(doc.control_number)
			else "and TCB has not set the number up either"
		)
	else:
		advice = "It can still take money; retried hourly. If this continues, ask Lockwood to {} {} by hand.".format(
			"expire" if status == "EXPIRED" else "suspend", doc.ml_reference
		)
	doc.flag_attention(
		"{}: {} (HTTP {}). {}".format(flag, message or "no answer", sent.http_status or "-", advice)
	)
	frappe.db.commit()
	return frappe._dict(ok=False, status="Failed", message=message or "no answer")


def _replace_closed_bill(doc):
	"""A reopened order whose bill Malipopay closed on their side: raise a new
	bill for what is still owed. Never throws."""
	try:
		so = frappe.get_doc("Sales Order", doc.sales_order)
		owed = _amount_still_owed(so)
		if owed > 0:
			_stage_collection_for_sales_order(so, amount=owed)
			frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			frappe.get_traceback(), "Malipopay replacement bill failed: " + cstr(doc.sales_order)
		)


def _mark_killed(doc, provider_status, note):
	doc.provider_status = provider_status
	if cstr(doc.attention_reason).startswith((KILL_FLAG, SUSPEND_FLAG, RESUME_FLAG)):
		doc.needs_attention = 0
		doc.attention_reason = ""
	doc.set_state("Dead", note)
	frappe.db.commit()
	# A payment that landed just before the kill, whose webhook was lost, would
	# be missed: the daily sweep skips Dead bills. Read the receipts once more.
	try:
		book_receipts_for_collection(doc.name)
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			frappe.get_traceback(), "Malipopay final receipt check after kill failed: " + doc.name
		)


def kill_pending_collections(limit=50):
	"""Hourly: bring every bill in line with its order, then retry every move
	that has not landed. A cancelled order, one closed for good, one whose
	contract was terminated, or one closed with nothing paid gets its bill
	expired; a closed order holding money gets it suspended (a bill already
	suspended is left alone unless the order is stopped for good)."""
	settings = get_malipopay_settings()
	if not settings.enabled:
		return
	closed_for_good = (
		f"ifnull(so.{CLOSED_FOR_GOOD_FIELD}, 0) = 1"
		if frappe.db.has_column("Sales Order", CLOSED_FOR_GOOD_FIELD)
		else "0"
	)
	stopped = frappe.db.sql(
		f"""
		select mpc.name
		from `tabMalipopay Collection` mpc
		join `tabSales Order` so on so.name = mpc.sales_order
		where mpc.state not in ('Dead', 'Kill Requested')
		  and (so.docstatus = 2
		       or (so.status = 'Closed' and (mpc.state not in ('Suspended', 'Suspend Requested') or {closed_for_good}
		           or exists (select 1 from `tabPlot Contract` pc where pc.sales_order = so.name
		                      and pc.docstatus = 1 and pc.contract_status = 'Terminated'))))
		limit %s""",
		(limit,),
		pluck=True,
	)
	for name in stopped:
		try:
			mpc = frappe.get_doc("Malipopay Collection", name)
			if _order_holds_money(mpc.sales_order) and not _stopped_for_good(mpc.sales_order):
				request_suspend_for_collection(
					mpc, _kill_reason(mpc), enqueue=False
				)  # sent in the loop below
			else:
				request_kill_for_collection(
					mpc, _kill_reason(mpc), enqueue=False
				)  # expired in the loop below
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "Malipopay stop request failed: " + name)

	# On an OPEN order a paused bill is resumed while the order owes money (a
	# Reopen whose resume did not reach Malipopay). A fully paid order's bill
	# is left open (operator, 15 Oct 2026): Malipopay takes no more money once
	# the payments are reported to it, and an open bill hears about a reversed
	# payment, so the customer can pay again on the same number. Pausing
	# fully paid bills waits for the reversal and handover-reversal rules.
	open_bills = frappe.db.sql(
		f"""
		select mpc.name
		from `tabMalipopay Collection` mpc
		join `tabSales Order` so on so.name = mpc.sales_order
		join `tabSales Invoice` si on si.name = so.plot_sales_invoice
		where so.docstatus = 1 and so.status != 'Closed' and si.docstatus = 1
		  and not ({closed_for_good})
		  and mpc.state in ('Suspended', 'Suspend Requested') and si.outstanding_amount > 0
		limit %s""",
		(limit,),
		as_dict=True,
	)
	for row in open_bills:
		try:
			mpc = frappe.get_doc("Malipopay Collection", row.name)
			if not _stopped_for_good(mpc.sales_order):
				request_resume_for_collection(mpc, "the order owes money again", enqueue=False)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "Malipopay open-order bill update failed: " + row.name)

	for state, worker in (
		("Kill Requested", expire_collection),
		("Suspend Requested", suspend_collection),
		("Resume Requested", resume_collection),
	):
		for name in frappe.get_all(
			"Malipopay Collection",
			filters={"state": state},
			order_by="modified asc",
			limit=limit,
			pluck="name",
		):
			try:
				worker(name)
			except Exception:
				frappe.db.rollback()
				frappe.log_error(frappe.get_traceback(), f"Malipopay {state} retry failed: {name}")


# Registration runs in a worker after the order is saved (about 15 seconds
# when Malipopay answers normally). Within this window a bill that is not yet
# Active is still on its way, not failed.
REGISTERING_GRACE_MINUTES = 5


@frappe.whitelist()
def sales_order_malipopay_notice(sales_order):
	"""The Sales Order form's Malipopay line: {message, collection,
	waiting_for_tcb, indicator, pending} when the customer's ways to pay are
	not all working, else {}. pending=True means registration is still in
	progress — the form asks again shortly. It also warns when the order was
	sold through Malipopay while TCB was down and TCB has not set the number up
	yet, so nobody gives the customer TCB payment instructions. Never throws —
	the Sales Order form must open whatever happens here."""
	try:
		if not frappe.has_permission("Sales Order", "read", sales_order):
			return {}
		so = frappe.db.get_value(
			"Sales Order",
			sales_order,
			["docstatus", "status", "control_number", "plot", "plot_application", "land_acquisition"],
			as_dict=True,
		)
		if not so or so.docstatus != 1 or so.status in ("Closed", "Completed") or not so.control_number:
			return {}
		if not collects_via_malipopay(so):
			return {}
		if not get_malipopay_settings().enabled:
			return {}
		tcb_ok = _tcb_takes_payment(so.control_number)
		waiting = None if tcb_ok else _waiting_for_tcb(so.control_number)
		only_tcb = (
			_("the customer can only pay through TCB")
			if tcb_ok
			else _("the customer cannot pay at all yet: TCB has not set this number up either")
		)
		bill = frappe.db.get_value(
			"Malipopay Collection",
			{"control_number": so.control_number},
			["name", "state", "needs_attention", "attention_reason", "modified"],
			as_dict=True,
			order_by="creation desc",
		)
		if not bill:
			# sold before Malipopay was switched on for the project: it stays
			# with TCB, as it always was, so there is nothing to say
			return {}
		if bill.state in ("Pending Create", "Creating"):
			# "modified" is when it entered this state, so a fresh Retry Registration counts as fresh
			if get_datetime(bill.modified) > add_to_date(now_datetime(), minutes=-REGISTERING_GRACE_MINUTES):
				return {
					"message": _("Registering with Malipopay…"),
					"collection": bill.name,
					"waiting_for_tcb": waiting,
					"indicator": "blue",
					"pending": True,
				}
			message = _(
				"Registration with Malipopay is taking too long — {0} for now. Use Retry Registration on"
			).format(only_tcb)
		elif bill.state == "Create Failed":
			message = _("Not registered with Malipopay — {0}. Use Retry Registration on").format(only_tcb)
		elif bill.state == "Dead":
			message = _("The Malipopay bill is closed — {0}. See").format(only_tcb)
		elif bill.state == "Kill Requested":
			message = _("The Malipopay bill is being expired. See")
		elif bill.state == "Needs Attention" or cint(bill.needs_attention):
			message = _("The Malipopay bill needs attention: {0}. See").format(
				cstr(bill.attention_reason)[:200]
			)
		elif not tcb_ok:
			# the bill works but TCB does not have the number yet (sold while TCB was down)
			return {
				"message": _(
					"Not set up at TCB yet (TCB was not answering when this order was submitted). "
					"The customer can pay through Malipopay now — do not give TCB payment instructions until this "
					"note is gone. The TCB step is in"
				),
				"collection": None,
				"waiting_for_tcb": waiting,
				"indicator": "orange",
			}
		else:
			return {}
		return {
			"message": message,
			"collection": bill.name,
			"waiting_for_tcb": waiting,
			"indicator": "orange" if tcb_ok else "red",
		}
	except Exception:
		# the form still opens with no banner, but the fault is recorded
		frappe.log_error(frappe.get_traceback(), f"Malipopay Sales Order banner failed: {sales_order}")
		return {}


def _tcb_takes_payment(control_number):
	"""True when the customer can pay this number at TCB: the number is live
	there (Registered, or already paid on). When the TCB integration is not
	Live, TCB is not one of the ways to pay and nothing is claimed about it."""
	from landms.tcb import _get_tcb_settings

	settings = _get_tcb_settings()
	if not cint(settings.get("enabled")) or settings.get("outbound_mode") != "Live":
		return True
	return frappe.db.get_value("TCB Control Number", control_number, "status") in ("Registered", "Paid")


def _tcb_fallback(control_number):
	"""What to tell the order's owner about TCB when Malipopay is not working."""
	if _tcb_takes_payment(control_number):
		return "The sale is safe — the customer can still pay through TCB."
	return "⚠ TCB has not set this number up either, so the customer cannot pay yet (see Waiting for TCB)."


def _waiting_for_tcb(control_number):
	"""The Waiting for TCB entry still open for registering this number, if any."""
	if not frappe.db.table_exists("Waiting for TCB"):
		return None
	return frappe.db.get_value(
		"Waiting for TCB",
		{
			"control_number": control_number,
			"action": "Register",
			"status": ["in", ["Waiting", "Needs a Person"]],
		},
		"name",
	)


def _amount_still_owed(so):
	"""Whole shillings the customer still owes on this order: the plot
	invoice's outstanding once it exists, else the order total less advances."""
	si = so.get("plot_sales_invoice")
	if si and frappe.db.get_value("Sales Invoice", si, "docstatus") == 1:
		return cint(flt(frappe.db.get_value("Sales Invoice", si, "outstanding_amount")))
	total = flt(so.get("rounded_total") or so.get("grand_total"))
	return cint(total - flt(so.get("advance_paid")))


def reopen_collection_for_sales_order(sales_order_name):
	"""A reopened order (sales_order_hooks.reopen_sales_order) needs its bill
	back. A paused bill is resumed — same Malipopay reference; a bill that was
	never expired is simply kept; a dead one is replaced by a new bill (their
	EXPIRED is final). Never throws."""
	try:
		so = frappe.get_doc("Sales Order", sales_order_name)
		if not so.get("control_number"):
			return
		live = frappe.db.get_all(
			"Malipopay Collection",
			filters={"control_number": so.control_number, "state": ("!=", "Dead")},
			pluck="name",
		)
		for name in live:
			lock_name = ("mp_kill:" + name)[:64]
			locked = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
			if not locked or not locked[0][0]:
				continue
			try:
				frappe.db.commit()
				mpc = frappe.get_doc("Malipopay Collection", name)
				if mpc.state == "Kill Requested":
					if cstr(mpc.attention_reason).startswith(KILL_FLAG):
						mpc.needs_attention = 0
						mpc.attention_reason = ""
					mpc.set_state(
						"Active",
						f"Sales Order {so.name} reopened before the bill was expired — bill kept.",
					)
					frappe.db.commit()
				elif mpc.state in ("Suspend Requested", "Suspended"):
					if cstr(mpc.attention_reason).startswith(SUSPEND_FLAG):
						mpc.needs_attention = 0
						mpc.attention_reason = ""
					request_resume_for_collection(mpc, f"Sales Order {so.name} reopened")
					frappe.db.commit()
			finally:
				frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
		if not live:
			# The old bill is gone with what was paid on it, so the new bill asks
			# only what is still owed — never the whole price again.
			owed = _amount_still_owed(so)
			if owed > 0:
				_stage_collection_for_sales_order(so, amount=owed)
				frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(
			frappe.get_traceback(),
			f"Malipopay reopen failed (reopen unaffected): {sales_order_name}",
		)


@frappe.whitelist(methods=["POST"])
def close_for_good_at_malipopay(sales_order_name):
	"""Sales Order button, System Manager only (like Reopen): end a Closed
	order's Malipopay bill for good. Its paused bill is expired — final at
	Malipopay — and the order is marked so LandMS refuses to reopen it; a
	customer who comes back is a new sale. TCB is not touched: its number was
	already released when the order was closed."""
	frappe.only_for("System Manager")
	so = frappe.db.get_value(
		"Sales Order",
		sales_order_name,
		["name", "docstatus", "status", "control_number"],
		as_dict=True,
		for_update=True,
	)
	if not so:
		frappe.throw(_("Sales Order {0} not found.").format(sales_order_name))
	if so.docstatus != 1 or so.status != "Closed":
		frappe.throw(_("Only a Closed Sales Order can be closed for good at Malipopay."))
	if not frappe.db.has_column("Sales Order", CLOSED_FOR_GOOD_FIELD):
		frappe.throw(_("The Closed for Good field is missing on Sales Order — run a migrate first."))
	if is_closed_for_good(so.name):
		frappe.throw(_("Sales Order {0} is already closed for good at Malipopay.").format(so.name))
	live = (
		frappe.db.get_all(
			"Malipopay Collection",
			filters={"control_number": so.control_number, "state": ("!=", "Dead")},
			pluck="name",
		)
		if so.control_number
		else []
	)
	if not live:
		frappe.throw(_("Sales Order {0} has no open Malipopay bill to close.").format(so.name))

	user = frappe.session.user
	frappe.db.set_value("Sales Order", so.name, CLOSED_FOR_GOOD_FIELD, 1, update_modified=True)
	bills = []
	for name in live:
		mpc = frappe.get_doc("Malipopay Collection", name)
		request_kill_for_collection(mpc, f"Sales Order {so.name} closed for good in LandMS by {user}")
		bills.append(mpc.ml_reference or mpc.name)
	note = _(
		"Closed for good at Malipopay by {0}. This order can no longer be reopened; a returning customer needs a new order."
	).format(user)
	if bills:
		note += " " + _("Bill being expired at Malipopay: {0}.").format(", ".join(bills))
	frappe.get_doc("Sales Order", so.name).add_comment("Comment", note)
	return {"ok": True, "message": note}


# ---------------------------------------------------------------------------
# Booking — breakdown receipts → Payment Receipt Ledger → Payment Entry
# ---------------------------------------------------------------------------
#
# The webhook is a doorbell, never a source of amounts: its `amount` is
# CUMULATIVE. Money is booked from GET /breakdown/{ML} — one row per
# receivedEvents[] entry, keyed on providerTxnId (= the webhook's
# transactionId; proven identical on the first real payment, 2026-09-15).
# ⚠ receivedEvents[].amount is ALSO a running total (proven on the second
# real payment the same day: 200 paid, receipt said 400, summary.paid 400),
# so a receipt's own money is the difference from the receipt before it —
# see _per_receipt_amounts, which cross-checks every reading against
# summary.paid and refuses to book when the figures do not add up.
#
# Every receipt is written to the Payment Receipt Ledger and COMMITTED before
# its Payment Entry is attempted (write-ahead). A crash mid-booking, a bill
# with no active order, or a missing bank account therefore leaves a Suspense
# row — money that is recorded and visible, never lost — and a receipt can
# never be booked twice, even after its Payment Entry is cancelled by hand.
#
# The Payment Entry itself goes through the same function TCB uses
# (payment_sync.create_payment_entry_for_sales_order), so the whole chain —
# plot Sales Invoice on first advance, government-share JE settled out of
# LandMS Settings.malipopay_bank_account, contract sync, Sales Order advance —
# is identical to a TCB payment. The PE is NAMED with Malipopay's transaction
# id, as TCB PEs are named with TCB's reference (operator decision
# 2026-09-15), with one guard TCB lacks: an id that is not a plain token falls
# back to normal naming instead of failing the booking.

_SAFE_PE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,59}$")
_LEDGER_FINAL = ("Booked", "Suppressed", "Voided by Operator")


def _safe_as_pe_name(txn_id):
	"""True when the provider id can safely be the Payment Entry's primary key:
	a plain token of bounded length that is not shaped like an ERPNext series."""
	txn_id = cstr(txn_id)
	if not _SAFE_PE_NAME.match(txn_id) or txn_id.upper().startswith("ACC-"):
		return False
	# a cancelled entry keeps its name, so a corrected re-booking of the same
	# receipt is named by ERPNext instead — still findable by reference_no
	return not frappe.db.exists("Payment Entry", txn_id)


def _paid_at_datetime(paid_at):
	"""Malipopay's paidAt is ISO-8601 UTC ('2026-09-15T07:14:47.641Z') ->
	naive datetime in the site's timezone, or None when absent/unparseable."""
	if not paid_at:
		return None
	try:
		parsed = datetime.fromisoformat(cstr(paid_at).strip().replace("Z", "+00:00"))
		if parsed.tzinfo is not None:
			parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
		return convert_utc_to_system_timezone(parsed).replace(tzinfo=None)
	except Exception:
		return None


def _posting_date_from_paid_at(paid_at):
	when = _paid_at_datetime(paid_at)
	return getdate(when) if when else getdate(today())


def fetch_breakdown(ml_reference, settings=None, *, log_links=None):
	"""GET /api/v1/payment/breakdown/{ML} — the itemised receipts. Returns
	{ok, data, http_status, raw}; data is the response's `data` object
	(payment / summary / receivedEvents / ledger)."""
	settings = settings or get_malipopay_settings()
	log_links = log_links or {}
	url = settings.base_url + BREAKDOWN_PATH + cstr(ml_reference)
	session = get_request_session()
	try:
		response = session.get(
			url,
			headers=_headers(settings),
			timeout=(settings.connect_timeout, settings.read_timeout),
			verify=bool(settings.verify_ssl),
		)
		body = _parse_json_or_text(response)
		ok = bool(response.ok and isinstance(body, dict) and body.get("success"))
		create_malipopay_api_log(
			direction="Outbound",
			event_type="Breakdown",
			status="Success" if ok else "Failed",
			processing_mode="Live",
			endpoint=url,
			http_status_code=response.status_code,
			ml_reference=ml_reference,
			response_payload=body,
			**log_links,
		)
		data = body.get("data") if ok and isinstance(body.get("data"), dict) else {}
		return frappe._dict(ok=ok, data=data or {}, http_status=response.status_code, raw=body)
	except Exception:
		create_malipopay_api_log(
			direction="Outbound",
			event_type="Breakdown",
			status="Failed",
			processing_mode="Live",
			endpoint=url,
			ml_reference=ml_reference,
			error=frappe.get_traceback(),
			**log_links,
		)
		return frappe._dict(ok=False, data={}, http_status=None, raw=None)


def book_receipts_for_collection(collection_name, *, webhook_event=None):
	"""Record every receipt on one collection in the ledger, then book each
	one that is not yet booked. Idempotent and re-runnable. Returns
	{ok, booked: [PE names], skipped, suspense, failed, message}."""
	settings = get_malipopay_settings()
	doc = frappe.get_doc("Malipopay Collection", collection_name)
	result = frappe._dict(ok=True, booked=[], skipped=0, suspense=0, failed=0, message="")

	if not settings.enabled or settings.inbound_mode != "Apply Payment":
		result.update(ok=False, message=f"inbound_mode is {settings.inbound_mode} — nothing booked")
		return result
	if cint(doc.get("maintenance_hold")):
		# an operator is working on this order's money: every path (hourly
		# repair, Check Payments, the nightly check, the receipt read after an
		# expiry) waits until the hold is cleared
		result.update(ok=False, message=f"collection {doc.name} is on Maintenance Hold — nothing booked")
		return result
	if not doc.ml_reference:
		result.update(ok=False, message=f"collection {doc.name} has no Malipopay reference")
		return result

	found = fetch_breakdown(
		doc.ml_reference,
		settings,
		log_links={
			"malipopay_collection": doc.name,
			"sales_order": doc.sales_order,
			"control_number": doc.control_number,
		},
	)
	if not found.ok:
		result.update(
			ok=False,
			message=f"breakdown fetch failed for {doc.ml_reference} — see Malipopay API Log",
		)
		return result
	receipts = found.data.get("receivedEvents") or []
	summary = found.data.get("summary") or {}
	payment = found.data.get("payment") or {}

	rows, reading, problem = _per_receipt_amounts(receipts, summary)
	if problem:
		doc.flag_attention("receipts not booked: " + problem)
		frappe.db.commit()
		result.update(
			ok=False,
			message=f"receipt amounts could not be established ({problem}) — nothing booked",
		)
		return result

	# Anything that stops a booking right now. Receipts are still recorded —
	# money with no place to land is exactly what the ledger is for.
	blocker = ""
	so_name = doc.sales_order
	bank_account = frappe.db.get_single_value("LandMS Settings", "malipopay_bank_account")
	so = (
		frappe.db.get_value(
			"Sales Order", so_name, ["name", "docstatus", "status", "control_number"], as_dict=True
		)
		if so_name
		else None
	)
	if not bank_account:
		blocker = "LandMS Settings is missing Malipopay Bank Account"
	elif not so or so.docstatus != 1 or so.status in ("Closed", "Cancelled"):
		blocker = "no active Sales Order for this collection ({})".format((so and so.status) or "missing")
	elif so.control_number and so.control_number != doc.control_number:
		blocker = f"Sales Order {so.name} carries control number {so.control_number}, the collection has {doc.control_number}"

	lock_name = ("mp_book:" + doc.name)[:64]
	locked = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock_name, 10))
	if not locked or not locked[0][0]:
		result.update(ok=False, message="another worker is booking this collection")
		return result
	try:
		frappe.db.commit()  # fresh snapshot after the wait
		for txn_id, amount, receipt in rows:
			if not txn_id or amount <= 0:
				result.failed += 1
				continue
			ledger_name, state = _ensure_ledger_row(
				doc,
				so_name,
				txn_id,
				amount,
				receipt,
				webhook_event,
				note="recorded from breakdown ({} reading, provider figure {}); booking pending".format(
					reading, receipt.get("amount")
				),
			)
			if state in _LEDGER_FINAL:
				result.skipped += 1
				continue
			if blocker:
				_note_ledger(ledger_name, "not booked: " + blocker)
				result.suspense += 1
				continue
			outcome, pe_name = _book_one_receipt(
				doc, so_name, txn_id, amount, receipt, bank_account, ledger_name, breakdown=found.data
			)
			if outcome == "booked":
				result.booked.append(pe_name)
			elif outcome == "skipped":
				result.skipped += 1
			else:
				result.failed += 1

		# provider snapshot on the register row — display only, never drives closure
		values = {"last_breakdown_at": now(), "last_synced_at": now()}
		if summary.get("paid") is not None:
			values["provider_paid_amount"] = flt(summary.get("paid"))
		if summary.get("fullyPaid") is not None:
			values["provider_fully_paid"] = 1 if summary.get("fullyPaid") else 0
		if payment.get("status"):
			values["provider_status"] = cstr(payment.get("status"))[:60]
		if not blocker and not result.failed:
			values.update({"needs_attention": 0, "attention_reason": ""})
		frappe.db.set_value("Malipopay Collection", doc.name, values, update_modified=False)
		frappe.db.commit()
		complete_waiting_settlements(doc.name, found.data)
	finally:
		try:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock_name,))
		except Exception:
			pass

	if blocker and result.suspense:
		doc.flag_attention(blocker)
		frappe.db.commit()
		result.update(ok=False, message=f"{result.suspense} receipt(s) held in suspense: {blocker}")
	if result.failed:
		result.update(
			ok=False,
			message=f"{result.failed} receipt(s) could not be booked — see Payment Receipt Ledger and Error Log",
		)
	return result


def _per_receipt_amounts(receipts, summary):
	"""Turn Malipopay's receivedEvents[] into [(txn_id, own_amount, receipt)].

	Their `amount` on a receipt is the RUNNING TOTAL at that receipt (proven
	2026-09-15), so each receipt's own money is the difference from the one
	before it, in paidAt order. Because nothing in their contract says so, the
	reading is cross-checked against summary.paid: if the figures instead ADD
	UP to it they are per-receipt and are used as they are; if neither reading
	fits (a refund, a reordering, a contract change) nothing is booked.
	Returns (rows, reading, problem) — problem is '' when rows are usable."""
	ordered = sorted(receipts, key=lambda r: cstr(r.get("paidAt") or ""))
	ids = [cstr(r.get("providerTxnId") or r.get("transactionId") or "").strip() for r in ordered]
	amounts = [flt(r.get("amount")) for r in ordered]
	if not ordered:
		return [], "none", ""
	paid = flt(summary.get("paid")) if summary.get("paid") is not None else None

	def fits(a, b):
		return abs(flt(a) - flt(b)) < 0.005

	per_receipt_fits = paid is not None and fits(sum(amounts), paid)
	cumulative_fits = paid is None or fits(amounts[-1], paid)

	if len(ordered) > 1 and per_receipt_fits and not cumulative_fits:
		return list(zip(ids, amounts, ordered, strict=False)), "per-receipt", ""
	if cumulative_fits:
		rows, running = [], 0.0
		for txn_id, cum, receipt in zip(ids, amounts, ordered, strict=False):
			delta = cum - running
			if delta <= 0:
				return (
					[],
					"cumulative",
					"running total does not increase at receipt {} ({} after {})".format(
						txn_id or "?", cum, running
					),
				)
			rows.append((txn_id, delta, receipt))
			running = cum
		return rows, "cumulative", ""
	return (
		[],
		"unknown",
		f"receipt amounts {amounts} fit neither a running total nor a sum against summary.paid {paid}",
	)


def _ensure_ledger_row(doc, so_name, txn_id, amount, receipt, webhook_event, note=None):
	"""Write-ahead: the receipt becomes a committed ledger fact before any
	booking is attempted. Returns (ledger name, current status)."""
	ledger_name = "PRL-Malipopay-" + txn_id
	row = frappe.db.get_value("Payment Receipt Ledger", ledger_name, ["name", "status"], as_dict=True)
	if row:
		return row.name, row.status
	ledger = frappe.get_doc(
		{
			"doctype": "Payment Receipt Ledger",
			"source_rail": "Malipopay",
			"txn_id": txn_id,
			"status": "Suspense",
			"reference": doc.control_number,
			"ml_reference": doc.ml_reference,
			"amount": amount,
			"paid_at": _paid_at_datetime(receipt.get("paidAt")),
			"channel": cstr(receipt.get("source") or "")[:140],
			"sales_order": so_name if so_name and frappe.db.exists("Sales Order", so_name) else None,
			"webhook_event": webhook_event
			if webhook_event and frappe.db.exists("Malipopay Webhook Event", webhook_event)
			else None,
			"malipopay_collection": doc.name,
			"notes": note or "recorded from breakdown; booking pending",
		}
	)
	ledger.flags.ignore_permissions = True
	ledger.insert()
	frappe.db.commit()
	return ledger.name, ledger.status


def _note_ledger(ledger_name, note):
	frappe.db.set_value(
		"Payment Receipt Ledger", ledger_name, "notes", (note or "")[:2000], update_modified=False
	)
	frappe.db.commit()


def _mark_receipt_booked(ledger_name, pe_name, note):
	govt_je = frappe.db.get_value("Journal Entry", {"lms_payment_entry": pe_name, "docstatus": 1}, "name")
	ledger = frappe.get_doc("Payment Receipt Ledger", ledger_name)
	ledger.status = "Booked"
	ledger.payment_entry = pe_name
	ledger.govt_je = govt_je
	ledger.notes = note
	ledger.flags.ignore_permissions = True
	ledger.save()
	frappe.db.commit()


def _book_one_receipt(doc, so_name, txn_id, amount, receipt, bank_account, ledger_name, breakdown=None):
	"""One receipt -> one Payment Entry through the TCB chain.
	Returns ("booked", pe) / ("skipped", pe) / ("failed", None)."""
	from landms.payment_sync import record_payment_waiting_for_others

	try:
		# waits for, then records after, any other payment on this order at the same moment
		pe_name = record_payment_waiting_for_others(
			sales_order_name=so_name,
			amount=amount,
			payment_date=cstr(_posting_date_from_paid_at(receipt.get("paidAt"))),
			bank_account=bank_account,
			reference_no=txn_id,
			remarks=f"Malipopay Payment — {so_name} / Control No: {doc.control_number} / Bill {doc.ml_reference} / Txn {txn_id}",
			submit=True,
			name_from_reference=_safe_as_pe_name(txn_id),
		)
	except Exception as exc:
		frappe.db.rollback()
		message = cstr(exc)
		if "Duplicate payment reference" in message:
			# already booked outside this path (e.g. by hand) — adopt, never double-book
			existing = frappe.db.get_value("Payment Entry", {"reference_no": txn_id, "docstatus": 1}, "name")
			if existing:
				_mark_receipt_booked(ledger_name, existing, "adopted existing Payment Entry")
				_record_settlement(ledger_name, existing, amount, doc, receipt, breakdown)
				return "skipped", existing
		frappe.log_error(frappe.get_traceback(), "Malipopay booking failed: " + ledger_name)
		_note_ledger(ledger_name, "booking failed: " + message)
		frappe.get_doc("Malipopay Collection", doc.name).flag_attention(
			f"receipt {txn_id} not booked: {message[:300]}"
		)
		frappe.db.commit()
		return "failed", None

	_mark_receipt_booked(ledger_name, pe_name, "booked from breakdown receipt")
	_record_settlement(ledger_name, pe_name, amount, doc, receipt, breakdown)
	return "booked", pe_name


SETTLEMENT_WAITING = "Waiting for Malipopay"


def _record_settlement(ledger_name, pe_name, amount, doc, receipt, breakdown=None):
	"""Malipopay's cut of one payment, as a record only (operator, 14 Oct
	2026). The figures are Malipopay's own, from the bill breakdown read to
	book this payment (see _malipopay_cut). When Malipopay has not stated them
	yet, the record waits and the next read of the bill fills it in
	(complete_waiting_settlements). No accounting entry. Runs after the
	booking has been saved, so it can never undo or stop a booking. Never
	throws."""
	try:
		if not frappe.db.table_exists("Malipopay Settlement"):
			return
		if frappe.db.exists("Malipopay Settlement", {"receipt": ledger_name}):
			return
		breakdown = breakdown or {}
		gross = flt(amount)
		cut = _malipopay_cut(breakdown, gross, (receipt or {}).get("paidAt"))
		settlement = frappe.get_doc(
			{
				"doctype": "Malipopay Settlement",
				"status": "Recorded" if cut else SETTLEMENT_WAITING,
				"receipt": ledger_name,
				"payment_entry": pe_name,
				"receipt_date": _posting_date_from_paid_at((receipt or {}).get("paidAt")),
				"sales_order": doc.sales_order,
				"customer": doc.get("customer"),
				"control_number": doc.control_number,
				"malipopay_collection": doc.name,
				"gross_amount": gross,
				"fee_percent": cut.percent if cut else 0,
				"fee_amount": cut.fee if cut else 0,
				"net_amount": gross - (cut.fee if cut else 0),
				"fee_source": cut.source if cut else "",
				"transaction_fee": flt((breakdown.get("payment") or {}).get("transactionFee")),
				"remarks": cut.remarks
				if cut
				else "Malipopay had not stated its cut yet when this payment was booked; "
				"filled in on the next read of the bill.",
			}
		)
		settlement.flags.ignore_permissions = True
		settlement.insert()
		frappe.db.commit()
	except Exception:
		frappe.db.rollback()
		frappe.log_error(frappe.get_traceback(), "Malipopay settlement record failed: " + cstr(ledger_name))


def complete_waiting_settlements(collection_name, breakdown):
	"""Fill in, from a later read of the bill, the settlements booked before
	Malipopay had stated its cut, and upgrade a cut worked out from the
	percentage to the one Malipopay posted. A settlement marked Settled is
	never changed. Never throws."""
	try:
		if not breakdown or not frappe.db.table_exists("Malipopay Settlement"):
			return
		rows = frappe.db.sql(
			"""select name, fee_source, gross_amount, receipt
			from `tabMalipopay Settlement`
			where malipopay_collection = %s
			  and (status = %s or (status = 'Recorded' and fee_source = 'Fee Percent'))""",
			(collection_name, SETTLEMENT_WAITING),
			as_dict=True,
		)
		for row in rows:
			paid_at = frappe.db.get_value("Payment Receipt Ledger", row.receipt, "paid_at")
			gross = flt(row.gross_amount)
			cut = _malipopay_cut(breakdown, gross, paid_at)
			if not cut or (row.fee_source == "Fee Percent" and cut.source != "Malipopay Ledger"):
				continue
			frappe.db.set_value(
				"Malipopay Settlement",
				row.name,
				{
					"status": "Recorded",
					"fee_percent": cut.percent,
					"fee_amount": cut.fee,
					"net_amount": gross - cut.fee,
					"fee_source": cut.source,
					"remarks": cut.remarks,
				},
			)
			frappe.db.commit()
	except Exception:
		frappe.log_error(
			frappe.get_traceback(), "Malipopay settlement completion failed: " + cstr(collection_name)
		)


def _malipopay_cut(breakdown, gross, paid_at):
	"""Malipopay's cut of one payment from a bill breakdown: the one its own
	ledger posted for the payment (live test ML330030: 500 paid = 495 to GVA +
	5 cut), else the payment x its percentage (feePercent). None while
	Malipopay has stated neither: on a bill's first payment both arrive a few
	seconds after the money (live test ML332079, 15 Oct 2026). The bill's
	running totals (feeAmount, netAmount) are not used: they lag too."""
	breakdown = breakdown or {}
	bill = breakdown.get("payment") or {}
	percent = flt(bill.get("feePercent"))
	stated = _cut_from_malipopay_ledger(breakdown.get("ledger"), gross, paid_at)
	if stated:
		fee, posted_at = stated
		return frappe._dict(
			fee=fee,
			percent=percent,
			source="Malipopay Ledger",
			remarks=f"Cut as posted in Malipopay's own ledger ({posted_at}).",
		)
	if bill.get("feePercent") is not None:
		return frappe._dict(
			fee=flt(gross * percent / 100, 2),
			percent=percent,
			source="Fee Percent",
			remarks="Malipopay's ledger had no line for this payment yet; cut worked out from its percentage.",
		)
	return None


def _cut_from_malipopay_ledger(ledger, amount, paid_at):
	"""(cut, postedAt) from the entry Malipopay posted on GVA's side for a
	payment of this amount: Malipopay Wallet (Assets, DR) + Platform Fees
	Expense (Expenses, DR) = Sales Revenue (CR) = the payment. Of several
	entries for the same amount, the one posted nearest the payment. None when
	there is no such entry or its lines do not add up to the payment.
	paid_at: Malipopay's ISO time, or the receipt's stored local datetime."""
	paid = paid_at if isinstance(paid_at, datetime) else _paid_at_datetime(paid_at)
	found = []
	for entry in ledger or []:
		if not isinstance(entry, dict):
			continue
		if entry.get("scope") != "MERCHANT" or entry.get("eventType") != "COLLECTION":
			continue
		if abs(flt(entry.get("totalCr")) - amount) >= 0.01:
			continue
		lines = [
			line for line in entry.get("lines") or [] if isinstance(line, dict) and line.get("side") == "DR"
		]
		cut = sum(flt(line.get("amount")) for line in lines if line.get("accountType") == "Expenses")
		kept = sum(flt(line.get("amount")) for line in lines if line.get("accountType") == "Assets")
		if cut < 0 or abs(cut + kept - amount) >= 0.01:
			continue
		posted = _paid_at_datetime(entry.get("postedAt"))
		distance = abs((posted - paid).total_seconds()) if posted and paid else float("inf")
		found.append((distance, cut, cstr(entry.get("postedAt"))))
	if not found:
		return None
	_distance, cut, posted_at = min(found, key=lambda row: row[0])
	return cut, posted_at


def _finish_event(event_name, status, message, payment_entry=None):
	values = {"processing_status": status, "processed_at": now(), "processing_message": (message or "")[:500]}
	if payment_entry:
		values["payment_entry"] = payment_entry
	frappe.db.set_value("Malipopay Webhook Event", event_name, values, update_modified=False)
	frappe.db.commit()


def process_webhook_event(event_name):
	"""Worker entry for one Pending inbox row: book its collection from the
	breakdown receipts, then close the row as Processed / Held / Failed."""
	event = frappe.get_doc("Malipopay Webhook Event", event_name)
	if event.processing_status != "Pending":
		return frappe._dict(ok=False, message=f"event is {event.processing_status}")
	if not event.malipopay_collection:
		_finish_event(event.name, "Held", "no collection linked — suspense until matched")
		return frappe._dict(ok=False, message="no collection")

	result = book_receipts_for_collection(event.malipopay_collection, webhook_event=event.name)
	own_pe = frappe.db.get_value(
		"Payment Receipt Ledger", "PRL-Malipopay-" + cstr(event.transaction_id), "payment_entry"
	)
	if result.ok:
		message = f"booked {len(result.booked)}, skipped {result.skipped}"
		_finish_event(event.name, "Processed", message, payment_entry=own_pe)
		if own_pe:
			so_name = frappe.db.get_value("Malipopay Collection", event.malipopay_collection, "sales_order")
			notify_so_owner(
				so_name,
				"Malipopay payment of <b>{}</b> on <b>{}</b> booked — Payment Entry {}.".format(
					frappe.format_value(event.cumulative_amount, {"fieldtype": "Currency"}),
					event.control_number,
					own_pe,
				),
				"green",
			)
	else:
		_finish_event(event.name, "Failed", result.message, payment_entry=own_pe)
	return result


def process_pending_webhook_events(limit=50):
	"""Hourly repair: re-run inbox rows the worker left Pending (crash, Redis
	outage) or Failed (transient breakdown/HTTP error). Held rows belong to
	an operator and are not touched."""
	settings = get_malipopay_settings()
	if not settings.enabled or settings.inbound_mode != "Apply Payment":
		return
	names = frappe.get_all(
		"Malipopay Webhook Event",
		filters={"processing_status": ["in", ["Pending", "Failed"]], "hmac_verified": 1},
		order_by="received_at asc",
		limit=limit,
		pluck="name",
	)
	for name in names:
		try:
			frappe.db.set_value(
				"Malipopay Webhook Event", name, "processing_status", "Pending", update_modified=False
			)
			frappe.db.commit()
			process_webhook_event(name)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "Malipopay repair failed: " + name)
