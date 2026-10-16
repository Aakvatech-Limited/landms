# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Report money taken outside Malipopay back to Malipopay — contract v1.1
section 11, POST /api/v1/ledger/external-entries (CREDIT / DEBIT).

Why: a plot order can carry a live Malipopay bill while the customer pays by
TCB or at the counter. Malipopay cannot see that money, so without a report
its bill keeps asking CRDB for the full amount and the customer can pay twice.

How it decides what to send — level, not events. For one bill it compares:
  * what LandMS holds against the order from every payment that did NOT come
    through Malipopay (Payment Ledger Entries still linked to the order's
    invoice or the order, excluding Payment Entries that the Payment Receipt
    Ledger records as Malipopay receipts), and
  * what LandMS has already reported (Sent/Replayed rows in Malipopay
    External Entry), checked against Malipopay's own summary.externalPaid,
and sends only the difference, per Payment Entry, DEBITs before CREDITs.
Because it works from the current state, a payment cancelled, unlinked,
re-linked or entered by hand all converge the same way, and the daily pass
repairs anything a trigger missed.

Limits honoured from the contract:
  * a CREDIT may not exceed what the bill still asks for (code 3158), so the
    total reported is capped at requested - paid-through-Malipopay;
  * amounts are whole shillings: each payment's amount is rounded DOWN, so
    LandMS never reports more than it holds;
  * a report never changes the bill's status; closure is read from
    summary.outstanding / summary.fullyPaid elsewhere.

Safety: everything is behind Malipopay Settings.external_entries_mode, which
ships Off and is separate from outbound_mode. Every trigger returns at once
when it is Off and can never raise into a payment, cancel or unreconcile.
A row is committed before its call, so a retry resends the same
external_reference and Malipopay replays instead of posting twice.
"""

import math

import frappe
from frappe.utils import cint, cstr, flt, get_datetime, get_request_session, now

from landms.malipopay import (
	_headers,
	_parse_json_or_text,
	collects_via_malipopay,
	create_malipopay_api_log,
	fetch_breakdown,
	get_malipopay_settings,
)

EXTERNAL_ENTRIES_PATH = "/api/v1/ledger/external-entries"
LIVE_STATES = ("Active", "Needs Attention")
MAX_ATTEMPTS = 8


# ---------------------------------------------------------------------------
# Switch
# ---------------------------------------------------------------------------


def get_mode():
	"""Off / Log Only / Live. Off unless Malipopay itself is enabled."""
	try:
		doc = frappe.get_cached_doc("Malipopay Settings")
		if not cint(doc.get("enabled")):
			return "Off"
		mode = doc.get("external_entries_mode") or "Off"
		return mode if mode in ("Off", "Log Only", "Live") else "Off"
	except Exception:
		return "Off"


# ---------------------------------------------------------------------------
# What LandMS holds outside Malipopay, and what it has already reported
# ---------------------------------------------------------------------------


def _invoices_for_sales_order(sales_order):
	return sorted(
		set(
			frappe.get_all(
				"Sales Invoice Item",
				filters={"sales_order": sales_order, "docstatus": 1},
				pluck="parent",
			)
		)
	)


def _malipopay_payment_entries(candidates):
	"""Of the candidate Payment Entries, the ones that ARE Malipopay money —
	never reported back. The Payment Receipt Ledger is the authority; the
	payment_rail stamp is honoured too where present."""
	if not candidates:
		return set()
	names = set(
		frappe.get_all(
			"Payment Receipt Ledger",
			filters={"source_rail": "Malipopay", "payment_entry": ["in", list(candidates)]},
			pluck="payment_entry",
		)
	)
	if frappe.db.has_column("Payment Entry", "payment_rail"):
		names.update(
			frappe.get_all(
				"Payment Entry",
				filters={"name": ["in", list(candidates)], "payment_rail": "Malipopay"},
				pluck="name",
			)
		)
	return names


def held_outside_malipopay(sales_order, since=None):
	"""[(payment_entry, whole_shillings, posting_date, rail)] in posting order,
	for every non-Malipopay Payment Entry still allocated to this order.

	since: the bill's creation. A bill asks for what was owed when it was made
	(a new sale: the whole order; a replacement after Reopen: only the balance),
	so money already attached to the order before then is outside its amount and
	is not reported again. "Attached" is when the payment's ledger rows against
	this order were written — not when the payment was made: a payment moved
	onto this order later (Payment Reconciliation) gets new rows then, and is
	reported."""
	targets = _invoices_for_sales_order(sales_order) + [sales_order]
	rows = frappe.get_all(
		"Payment Ledger Entry",
		filters={
			"voucher_type": "Payment Entry",
			"against_voucher_no": ["in", targets],
			"delinked": 0,
		},
		fields=["voucher_no", "amount", "creation"],
	)
	exclude = _malipopay_payment_entries({r.voucher_no for r in rows})
	totals, attached = {}, {}
	for r in rows:
		if r.voucher_no in exclude:
			continue
		totals[r.voucher_no] = totals.get(r.voucher_no, 0.0) - flt(r.amount)
		when = get_datetime(r.creation)
		if r.voucher_no not in attached or when < attached[r.voucher_no]:
			attached[r.voucher_no] = when
	out = []
	for pe_name, amount in totals.items():
		whole = int(math.floor(amount + 1e-6))
		if whole <= 0:
			continue
		pe = frappe.db.get_value(
			"Payment Entry", pe_name, ["docstatus", "posting_date", "creation", "payment_rail"], as_dict=True
		)
		if not pe or pe.docstatus != 1:
			continue
		if since and attached[pe_name] < get_datetime(since):
			continue
		out.append((pe_name, whole, pe.posting_date, cstr(pe.get("payment_rail")), cstr(pe.creation)))
	out.sort(key=lambda x: (cstr(x[2]), x[4], x[0]))
	return [(a, b, c, d) for a, b, c, d, _ in out]


def reported_so_far(collection_name):
	"""({payment_entry: net whole shillings reported}, [pending row names])."""
	net, pending = {}, []
	for r in frappe.get_all(
		"Malipopay External Entry",
		filters={"malipopay_collection": collection_name},
		fields=["name", "payment_entry", "entry_type", "amount", "status", "attempts"],
		order_by="creation asc",
	):
		if r.status in ("Sent", "Replayed"):
			sign = 1 if r.entry_type == "CREDIT" else -1
			net[r.payment_entry] = net.get(r.payment_entry, 0) + sign * cint(r.amount)
		elif r.status == "Queued" or (r.status == "Failed" and cint(r.attempts) < MAX_ATTEMPTS):
			pending.append(r.name)
	return net, pending


# ---------------------------------------------------------------------------
# The plan — a pure function, so it can be tested without Malipopay
# ---------------------------------------------------------------------------


def plan_entries(held, reported, requested, paid_via_malipopay):
	"""held: [(pe, amount, ...)] in order; reported: {pe: net}.
	Returns [(pe, "CREDIT"|"DEBIT", amount)] with DEBITs first."""
	room = max(0, cint(requested) - cint(paid_via_malipopay))
	remaining = min(sum(cint(h[1]) for h in held), room)
	target = {}
	for h in held:
		take = min(cint(h[1]), remaining)
		target[h[0]] = take
		remaining -= take
	for pe in reported:
		target.setdefault(pe, 0)
	debits, credits = [], []
	for pe, want in target.items():
		delta = want - cint(reported.get(pe, 0))
		if delta < 0:
			debits.append((pe, "DEBIT", -delta))
		elif delta > 0:
			credits.append((pe, "CREDIT", delta))
	order = {h[0]: i for i, h in enumerate(held)}
	debits.sort(key=lambda x: order.get(x[0], -1))
	credits.sort(key=lambda x: order.get(x[0], len(order)))
	return debits + credits


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------


def _next_external_reference(payment_entry, entry_type):
	n = frappe.db.count(
		"Malipopay External Entry", {"payment_entry": payment_entry, "entry_type": entry_type}
	)
	return f"{payment_entry}-{'C' if entry_type == 'CREDIT' else 'D'}{n + 1}"


def _queue_row(doc, pe_name, entry_type, amount, trigger, rail):
	pe = frappe.db.get_value("Payment Entry", pe_name, ["posting_date", "reference_no"], as_dict=True) or {}
	if entry_type == "CREDIT":
		description = f"LandMS {doc.sales_order} | Payment {pe_name} | Control No {doc.control_number}"
		reason = "Payment received outside Malipopay ({})".format(rail or "manual receipt")
	else:
		description = (
			f"LandMS {doc.sales_order} | Payment {pe_name} reversed | Control No {doc.control_number}"
		)
		reason = f"Payment {pe_name} cancelled or unlinked from this order in LandMS"
	row = frappe.get_doc(
		{
			"doctype": "Malipopay External Entry",
			"status": "Queued",
			"entry_type": entry_type,
			"amount": cint(amount),
			"external_reference": _next_external_reference(pe_name, entry_type),
			"malipopay_collection": doc.name,
			"sales_order": doc.sales_order,
			"payment_entry": pe_name,
			"ml_reference": doc.ml_reference,
			"control_number": doc.control_number,
			"source": (rail or "EXTERNAL").upper()[:40],
			"paid_at": get_datetime(pe.get("posting_date")) if pe.get("posting_date") else None,
			"trigger": cstr(trigger)[:140],
			"description": description[:250],
			"reason": reason[:250],
		}
	)
	row.flags.ignore_permissions = True
	row.insert()
	return row.name


def _payload(row):
	body = {
		"reference": row.ml_reference,
		"type": row.entry_type,
		"amount": cint(row.amount),
		"externalReference": row.external_reference,
		"description": " ".join(cstr(row.description).split()),
		"reason": " ".join(cstr(row.reason).split()),
		"source": row.source or "EXTERNAL",
	}
	if row.entry_type == "CREDIT" and row.paid_at:
		body["paidAt"] = get_datetime(row.paid_at).strftime("%Y-%m-%dT%H:%M:%SZ")
	return body


def send_entry(row_name, settings=None, *, session=None):
	"""One POST for one row. Owns its commit. Returns the new status."""
	settings = settings or get_malipopay_settings()
	row = frappe.get_doc("Malipopay External Entry", row_name)
	if row.status in ("Sent", "Replayed", "Refused"):
		return row.status
	url = settings.base_url + EXTERNAL_ENTRIES_PATH
	body = _payload(row)
	values = {"attempts": cint(row.attempts) + 1, "last_attempt_at": now()}
	log = dict(
		direction="Outbound",
		event_type="External Receipt",
		processing_mode="Live",
		endpoint=url,
		control_number=row.control_number,
		ml_reference=row.ml_reference,
		transaction_id=row.external_reference,
		sales_order=row.sales_order,
		malipopay_collection=row.malipopay_collection,
		payment_entry=row.payment_entry,
		request_payload=body,
	)
	try:
		session = session or get_request_session()
		response = session.post(
			url,
			json=body,
			headers=_headers(settings),
			timeout=(settings.connect_timeout, settings.read_timeout),
			verify=bool(settings.verify_ssl),
		)
		reply = _parse_json_or_text(response)
		data = reply.get("data") if isinstance(reply, dict) and isinstance(reply.get("data"), dict) else {}
		values.update(
			http_status=response.status_code,
			provider_code=cstr(reply.get("code") if isinstance(reply, dict) else "")[:40],
			message=cstr(reply.get("message") if isinstance(reply, dict) else "")[:500],
		)
		if response.ok and isinstance(reply, dict) and reply.get("success"):
			balance = data.get("balanceAfter") or {}
			values.update(
				status="Replayed" if data.get("idempotentReplay") else "Sent",
				entry_id=cstr(data.get("entryId"))[:140],
				outstanding_after=cint(balance.get("outstanding"))
				if balance.get("outstanding") is not None
				else None,
				external_paid_after=cint(balance.get("externalPaid"))
				if balance.get("externalPaid") is not None
				else None,
			)
		elif response.ok or (response.status_code and 400 <= response.status_code < 500):
			values["status"] = "Refused"
		else:
			values["status"] = "Failed"
		create_malipopay_api_log(
			status="Success" if values["status"] in ("Sent", "Replayed") else "Failed",
			http_status_code=response.status_code,
			response_payload=reply,
			**log,
		)
	except Exception:
		values.update(status="Failed", message="network or client error — will retry")
		create_malipopay_api_log(status="Failed", error=frappe.get_traceback(), **log)
	frappe.db.set_value("Malipopay External Entry", row_name, values)
	if values["status"] == "Refused":
		_flag(
			row.malipopay_collection,
			"External entry {} refused by Malipopay (code {}): {}".format(
				row.external_reference, values.get("provider_code") or "?", values.get("message") or ""
			),
		)
	frappe.db.commit()
	return values["status"]


def _flag(collection_name, reason):
	try:
		frappe.get_doc("Malipopay Collection", collection_name).flag_attention(cstr(reason)[:500])
	except Exception:
		frappe.log_error(
			frappe.get_traceback(), "Malipopay external: could not flag " + cstr(collection_name)
		)


# ---------------------------------------------------------------------------
# The pass for one bill
# ---------------------------------------------------------------------------


def sync_external_entries(collection_name, trigger="manual", *, session=None):
	"""Bring Malipopay's view of money taken outside it in line with LandMS for
	one bill. Safe to run any number of times. Returns a summary dict."""
	result = frappe._dict(collection=collection_name, mode=get_mode(), sent=[], planned=[], message="")
	if result.mode == "Off":
		result.message = "external_entries_mode is Off"
		return result
	settings = get_malipopay_settings()
	doc = frappe.get_doc("Malipopay Collection", collection_name)
	if doc.state not in LIVE_STATES or not doc.ml_reference:
		result.message = f"bill is {doc.state} — nothing to report"
		return result

	lock = ("mp_ext:" + doc.name)[:64]
	got = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (lock, 10))
	if not got or not got[0][0]:
		result.message = "another worker holds this bill"
		return result
	try:
		frappe.db.commit()
		reported, pending = reported_so_far(doc.name)
		if pending:
			if result.mode != "Live":
				result.message = f"{len(pending)} unsent row(s) wait for Live mode"
				return result
			for name in pending:
				send_entry(name, settings, session=session)
			reported, pending = reported_so_far(doc.name)
			if pending:
				result.message = f"{len(pending)} row(s) still unsent — nothing new planned"
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
			result.message = "breakdown fetch failed — nothing planned"
			return result
		# this read follows every booking within seconds: it also fills in a
		# settlement booked before Malipopay had stated its cut
		from landms.malipopay import complete_waiting_settlements

		complete_waiting_settlements(doc.name, found.data)
		summary = found.data.get("summary") or {}
		requested = cint(summary.get("requested"))
		paid_mp = cint(
			summary.get("paidViaMalipopay")
			if summary.get("paidViaMalipopay") is not None
			else summary.get("paid")
		)
		remote_external = cint(summary.get("externalPaid") or 0)
		ours = sum(reported.values())
		if remote_external != ours:
			_flag(
				doc.name,
				f"Malipopay shows {remote_external} reported as paid outside it; LandMS has reported {ours}. Nothing sent until this is explained.",
			)
			frappe.db.commit()
			result.message = f"mismatch: Malipopay externalPaid {remote_external}, LandMS reported {ours}"
			return result

		held = held_outside_malipopay(doc.sales_order, since=doc.creation)
		rails = {h[0]: h[3] for h in held}
		result.planned = plan_entries(held, reported, requested, paid_mp)
		if not result.planned:
			result.message = "in step"
			return result

		if result.mode == "Log Only":
			create_malipopay_api_log(
				direction="Outbound",
				event_type="External Receipt",
				status="Ignored",
				processing_mode="Log Only",
				control_number=doc.control_number,
				ml_reference=doc.ml_reference,
				sales_order=doc.sales_order,
				malipopay_collection=doc.name,
				request_payload={
					"trigger": trigger,
					"requested": requested,
					"paid_via_malipopay": paid_mp,
					"external_paid": remote_external,
					"held_outside": held,
					"would_send": result.planned,
				},
			)
			frappe.db.commit()
			result.message = f"Log Only — {len(result.planned)} entr(y/ies) would be sent"
			return result

		names = [
			_queue_row(doc, pe, kind, amount, trigger, rails.get(pe)) for pe, kind, amount in result.planned
		]
		frappe.db.commit()
		for i, name in enumerate(names):
			status = send_entry(name, settings, session=session)
			result.sent.append((name, status))
			if status == "Refused":
				for later in names[i + 1 :]:
					frappe.db.set_value(
						"Malipopay External Entry",
						later,
						{
							"status": "Refused",
							"message": "withdrawn unsent: an earlier entry in the same pass was refused; the next pass re-plans",
						},
					)
				frappe.db.commit()
				break
			if status != "Sent" and status != "Replayed":
				break
		result.message = "sent {} of {}".format(
			sum(1 for _, s in result.sent if s in ("Sent", "Replayed")), len(names)
		)
		return result
	finally:
		try:
			frappe.db.sql("SELECT RELEASE_LOCK(%s)", (lock,))
		except Exception:
			pass


# ---------------------------------------------------------------------------
# Triggers — never raise, return at once when the switch is Off
# ---------------------------------------------------------------------------


def _sales_orders_for(doctype, name):
	if doctype == "Sales Order":
		return {name}
	if doctype == "Sales Invoice":
		return set(frappe.get_all("Sales Invoice Item", filters={"parent": name}, pluck="sales_order")) - {
			None,
			"",
		}
	return set()


def _enqueue_for_sales_orders(sales_orders, trigger):
	for so in sales_orders:
		# Only a submitted plot sale on a Malipopay project can ever need a
		# section 11 entry; everything else stops here, before any bill lookup.
		if frappe.db.get_value("Sales Order", so, "docstatus") != 1 or not collects_via_malipopay(so):
			continue
		for name in frappe.get_all(
			"Malipopay Collection",
			filters={
				"sales_order": so,
				"state": ["in", list(LIVE_STATES)],
				"ml_reference": ["is", "set"],
			},
			pluck="name",
		):
			frappe.enqueue(
				"landms.malipopay_external.sync_external_entries",
				queue="short",
				job_id="mp_ext_" + name,
				deduplicate=True,
				enqueue_after_commit=True,
				collection_name=name,
				trigger=trigger,
			)


def on_payment_ledger_entry_submit(doc, method=None):
	"""Payment Ledger Entry on_submit: catches a Payment Entry submitted or
	re-linked (Payment Reconciliation) against a plot invoice or order."""
	try:
		if doc.get("voucher_type") != "Payment Entry" or doc.get("against_voucher_type") not in (
			"Sales Invoice",
			"Sales Order",
		):
			return
		if get_mode() == "Off":
			return
		_enqueue_for_sales_orders(
			_sales_orders_for(doc.against_voucher_type, doc.against_voucher_no),
			f"Payment Ledger Entry {doc.name} ({doc.voucher_no})",
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Malipopay external trigger failed (posting unaffected)")


def on_payment_entry_cancel(doc, method=None):
	"""Payment Entry on_cancel: the money is gone, so a DEBIT may be due."""
	try:
		if get_mode() == "Off":
			return
		orders = set()
		for ref in doc.get("references") or []:
			orders |= _sales_orders_for(ref.reference_doctype, ref.reference_name)
		_enqueue_for_sales_orders(orders, f"Payment Entry {doc.name} cancelled")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Malipopay external trigger failed (cancel unaffected)")


def on_unreconcile_payment_submit(doc, method=None):
	"""Unreconcile Payment on_submit: money unlinked from an order without any
	Payment Entry event (ERPNext delinks the ledger rows with a direct update)."""
	try:
		if get_mode() == "Off":
			return
		orders = set()
		for row in doc.get("allocations") or []:
			orders |= _sales_orders_for(row.reference_doctype, row.reference_name)
		_enqueue_for_sales_orders(orders, f"Unreconcile Payment {doc.name}")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Malipopay external trigger failed (unreconcile unaffected)")


def start_external_entries_sync():
	"""05:10 (hooks: cron) — hands the daily pass to the long queue: a timed
	job itself runs in the default queue, which stops a job after 5 minutes."""
	frappe.enqueue(
		"landms.malipopay_external.sync_all_external_entries",
		queue="long",
		timeout=3600,
		job_id="landms-malipopay-external-sync",
		deduplicate=True,
	)


def sync_all_external_entries():
	"""Daily pass over every live bill — the safety net for missed triggers."""
	if get_mode() == "Off":
		return
	for name in frappe.get_all(
		"Malipopay Collection",
		filters={
			"state": ["in", list(LIVE_STATES)],
			"ml_reference": ["is", "set"],
		},
		pluck="name",
	):
		try:
			sync_external_entries(name, "daily pass")
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "Malipopay external daily pass failed: " + name)


@frappe.whitelist(methods=["POST"])  # changes data / calls Malipopay or TCB
def sync_now(collection_name):
	"""Run the pass for one bill now (form button / console)."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	return sync_external_entries(collection_name, "manual by " + frappe.session.user)
