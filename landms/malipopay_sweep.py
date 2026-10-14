"""Malipopay daily sweep — level-triggered convergence, and the divergence report.

The webhook is only an accelerator: Malipopay delivers once, with no retry, so
a notification that never arrives is simply lost. The sweep is what makes that
survivable. It asks their side what it holds, compares it with our Payment
Receipt Ledger, books whatever is missing through the same breakdown path the
webhook uses, and writes a Malipopay Sweep Log whose child rows ARE the daily
three-way report (their ledger vs our receipts vs the GL).

TWO API FACTS, PROVEN AGAINST THEIR PRODUCTION API 2026-09-15, THAT SHAPE THIS:

1. `GET /api/v1/payment?dateFrom&dateTo` filters on **createdAt, not updatedAt**
   (ML251219, created 09-14 and paid 09-15, is absent from a 09-15 window). So
   sweeping "their recent window" would MISS the case the sweep exists for — a
   bill raised months ago and paid today. Phase 1 therefore drives from OUR
   registry: every collection that is not Dead is converged, whatever its age.
   Their listing is used only in Phase 2, for orphans — where createdAt is the
   right axis, because an orphan is defined by having been created at all.

2. Pagination ignores `page` and `offset`; only **`skip`** advances the cursor.

Watermark discipline (Malipopay Settings.sweep_watermark) matches the TCB
reconciliation log: it advances ONLY when a run completes, so a failed or
stopped run leaves the window open and the next run re-covers it.
"""

from __future__ import annotations

import time
from typing import Any

import frappe
from frappe.utils import add_to_date, cint, cstr, flt, get_datetime, now, now_datetime

from landms.malipopay import (
	QUERY_PATH,
	_headers,
	book_receipts_for_collection,
	create_malipopay_api_log,
	fetch_breakdown,
	get_malipopay_settings,
)

#: Re-cover this much before the watermark — a bill paid seconds before the last
#: run finished must not fall between two windows.
ORPHAN_OVERLAP_HOURS = 6
#: How far back an orphan scan reaches when no watermark has ever been set.
ORPHAN_COLD_START_DAYS = 30
#: Their pager caps out well below this; it exists to stop a runaway loop.
MAX_LISTING_PAGES = 200
PAGE_SIZE = 50


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


def start_daily_malipopay_sweep():
	"""04:40 (hooks: cron) — hands the daily sweep to the long queue: a timed
	job itself runs in the default queue, which stops a job after 5 minutes."""
	frappe.enqueue(
		"landms.malipopay_sweep.run_daily_malipopay_sweep",
		queue="long",
		timeout=3600,
		job_id="landms-malipopay-daily-sweep",
		deduplicate=True,
	)


def run_daily_malipopay_sweep():
	"""Scheduler entry (hooks.py). Honours the enable flag and never raises."""
	try:
		settings = get_malipopay_settings()
		if not settings.enabled or not cint(
			frappe.db.get_single_value("Malipopay Settings", "daily_sweep_enabled")
		):
			return
		run_malipopay_sweep(triggered_by="Scheduled")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Malipopay daily sweep failed")


def run_malipopay_sweep(triggered_by: str = "Manual", log_name: str | None = None) -> dict[str, Any]:
	"""Converge every live collection, adopt orphans, report divergences.

	Returns {ok, status, log, collections_seen, booked, amount, orphans,
	divergences, message}. Never raises: the log carries the outcome.
	"""
	t_start = time.time()
	settings = get_malipopay_settings()
	window_to = now_datetime()
	window_from = _window_start(
		frappe.db.get_single_value("Malipopay Settings", "sweep_watermark"), window_to
	)

	if log_name:
		log = frappe.get_doc("Malipopay Sweep Log", log_name)
		log.db_set(
			{
				"status": "Running",
				"started_at": now(),
				"stop_requested": 0,
				"window_from": window_from,
				"window_to": window_to,
			},
			update_modified=True,
		)
	else:
		log = frappe.get_doc(
			{
				"doctype": "Malipopay Sweep Log",
				"status": "Running",
				"triggered_by": triggered_by,
				"started_at": now(),
				"window_from": window_from,
				"window_to": window_to,
			}
		)
		log.flags.ignore_permissions = True
		log.insert()
	frappe.db.commit()

	counts = frappe._dict(seen=0, booked=0, amount=0.0, orphans=0, divergences=0, errors=0)
	stopped = False
	try:
		if not settings.enabled:
			return _finish(
				log,
				"Failed",
				"Malipopay is disabled in settings — nothing swept.",
				counts,
				t_start,
				advance=False,
			)

		stopped = _sweep_collections(log, settings, counts)
		if not stopped:
			stopped = _sweep_orphans(log, settings, counts, window_from, window_to)

		if stopped:
			return _finish(log, "Stopped", "Stopped on request.", counts, t_start, advance=False)

		status = "Completed" if not counts.errors else "Failed"
		message = (
			"Swept "
			+ str(counts.seen)
			+ " collection(s): booked "
			+ str(counts.booked)
			+ " receipt(s) worth "
			+ frappe.utils.fmt_money(counts.amount, currency="TZS")
			+ ", orphans "
			+ str(counts.orphans)
			+ ", divergences "
			+ str(counts.divergences)
			+ ", errors "
			+ str(counts.errors)
			+ "."
		)
		return _finish(
			log, status, message, counts, t_start, advance=(status == "Completed"), window_to=window_to
		)
	except Exception:
		frappe.db.rollback()
		error = frappe.get_traceback()
		frappe.log_error(error, "Malipopay sweep: unhandled")
		log.db_set({"error": error}, update_modified=False)
		return _finish(
			log, "Failed", "Unhandled sweep exception — see the error field.", counts, t_start, advance=False
		)


# ---------------------------------------------------------------------------
# Phase 1 — convergence, driven from OUR registry
# ---------------------------------------------------------------------------


def _sweep_collections(log, settings, counts) -> bool:
	"""Every non-Dead collection: compare their breakdown with our ledger and
	book what is missing. Returns True when the operator asked to stop."""
	names = frappe.get_all(
		"Malipopay Collection",
		filters={"state": ["!=", "Dead"], "ml_reference": ["!=", ""]},
		order_by="creation asc",
		pluck="name",
	)
	for name in names:
		if _stop_requested(log):
			return True
		counts.seen += 1
		try:
			_sweep_one_collection(log, settings, counts, name)
		except Exception:
			counts.errors += 1
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), "Malipopay sweep: " + name)
			_row(log, name, action="Error", note="sweep failed — see Error Log")
	return False


def _sweep_one_collection(log, settings, counts, name):
	doc = frappe.db.get_value(
		"Malipopay Collection",
		name,
		["name", "control_number", "ml_reference", "state", "maintenance_hold", "sales_order"],
		as_dict=True,
	)
	ledger_paid = _ledger_paid(name)

	if cint(doc.maintenance_hold):
		_row(
			log,
			name,
			doc=doc,
			action="Skipped (Hold)",
			ledger_paid=ledger_paid,
			note="maintenance hold — left for the operator",
		)
		return

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
		counts.errors += 1
		_row(
			log,
			name,
			doc=doc,
			action="Error",
			ledger_paid=ledger_paid,
			note="breakdown unreachable — window stays open, next run retries",
		)
		return
	provider_paid = flt((found.data.get("summary") or {}).get("paid"))

	if abs(provider_paid - ledger_paid) < 0.005:
		_row(log, name, doc=doc, action="Clean", provider_paid=provider_paid, ledger_paid=ledger_paid)
		return

	# They hold money we have not booked (or we hold more than they report).
	if provider_paid < ledger_paid:
		counts.divergences += 1
		_row(
			log,
			name,
			doc=doc,
			action="Divergence Flagged",
			provider_paid=provider_paid,
			ledger_paid=ledger_paid,
			note="our ledger exceeds their total by "
			+ frappe.utils.fmt_money(ledger_paid - provider_paid, currency="TZS")
			+ " — a reversal or a mis-booking; operator review",
		)
		frappe.get_doc("Malipopay Collection", name).flag_attention(
			"sweep: our ledger " + str(ledger_paid) + " exceeds their reported " + str(provider_paid)
		)
		frappe.db.commit()
		return

	if settings.inbound_mode != "Apply Payment":
		counts.divergences += 1
		_row(
			log,
			name,
			doc=doc,
			action="Divergence Flagged",
			provider_paid=provider_paid,
			ledger_paid=ledger_paid,
			note="inbound mode is " + cstr(settings.inbound_mode) + " — reported, not booked",
		)
		return

	result = book_receipts_for_collection(name)
	booked_now = _ledger_paid(name) - ledger_paid
	counts.booked += len(result.booked or [])
	counts.amount += booked_now
	if result.booked:
		_row(
			log,
			name,
			doc=doc,
			action="Booked",
			provider_paid=provider_paid,
			ledger_paid=_ledger_paid(name),
			txns=len(result.booked),
			amount=booked_now,
			note="booked from breakdown: " + ", ".join(result.booked),
		)
	else:
		counts.divergences += 1
		_row(
			log,
			name,
			doc=doc,
			action="Divergence Flagged",
			provider_paid=provider_paid,
			ledger_paid=ledger_paid,
			note=cstr(result.message) or "money at Malipopay could not be booked — receipts held in suspense",
		)


# ---------------------------------------------------------------------------
# Phase 2 — orphans on their side (their listing, windowed on createdAt)
# ---------------------------------------------------------------------------


def _sweep_orphans(log, settings, counts, window_from, window_to) -> bool:
	"""A bill at Malipopay with no local Collection row. Their listing omits
	customerReference, so the control number comes from the breakdown."""
	for row in _list_collections(settings, window_from, window_to):
		if _stop_requested(log):
			return True
		ml = cstr(row.get("reference") or "").strip()
		if not ml or frappe.db.exists("Malipopay Collection", {"ml_reference": ml}):
			continue
		counts.orphans += 1
		found = fetch_breakdown(ml, settings)
		control_number = cstr(
			((found.data.get("payment") or {}) if found.ok else {}).get("customerReference") or ""
		).strip()
		paid = flt((found.data.get("summary") or {}).get("paid")) if found.ok else 0
		sales_order = (
			frappe.db.get_value("Sales Order", {"control_number": control_number, "docstatus": 1}, "name")
			if control_number
			else None
		)
		_row(
			log,
			None,
			action="Orphan Adopted",
			ml_reference=ml,
			reference=control_number,
			provider_paid=paid,
			note=(
				"bill at Malipopay with no local collection"
				+ (
					" — control number " + control_number
					if control_number
					else " — and no customerReference on it"
				)
				+ (" — Sales Order " + cstr(sales_order) if sales_order else " — no matching Sales Order")
				+ (
					"; " + frappe.utils.fmt_money(paid, currency="TZS") + " already paid on it"
					if paid
					else ""
				)
				+ ". Operator review before anything is booked."
			),
		)
		create_malipopay_api_log(
			direction="Inbound",
			event_type="Sweep",
			status="Ignored",
			processing_mode=settings.inbound_mode,
			ml_reference=ml,
			control_number=control_number or None,
			sales_order=sales_order,
			response_payload={"orphan": True, "paid": paid, "sweep_log": log.name},
			error="orphan collection at Malipopay — no local Malipopay Collection row",
		)
	return False


def _list_collections(settings, window_from, window_to):
	"""Their collection listing over a createdAt window, paged with `skip`
	(their `page`/`offset` parameters are ignored — proven 2026-09-15)."""
	from frappe.utils import get_request_session

	session = get_request_session()
	url = settings.base_url + QUERY_PATH
	skip = 0
	for _page in range(MAX_LISTING_PAGES):
		params = {
			"dateFrom": cstr(window_from)[:10],
			"dateTo": cstr(window_to)[:10],
			"limit": PAGE_SIZE,
			"skip": skip,
		}
		try:
			response = session.get(
				url,
				params=params,
				headers=_headers(settings),
				timeout=(settings.connect_timeout, settings.read_timeout),
				verify=bool(settings.verify_ssl),
			)
			body = response.json() if response.ok else {}
			docs = ((body.get("data") or {}).get("docs")) or []
		except Exception:
			create_malipopay_api_log(
				direction="Outbound",
				event_type="Sweep",
				status="Failed",
				processing_mode="Live",
				endpoint=url,
				request_payload=params,
				error=frappe.get_traceback(),
			)
			return
		create_malipopay_api_log(
			direction="Outbound",
			event_type="Sweep",
			status="Success" if docs else "Ignored",
			processing_mode="Live",
			endpoint=url,
			http_status_code=response.status_code,
			request_payload=params,
			response_payload={"returned": len(docs)},
		)
		if not docs:
			return
		yield from docs
		if len(docs) < PAGE_SIZE:
			return
		skip += len(docs)


# ---------------------------------------------------------------------------
# pieces
# ---------------------------------------------------------------------------


def _window_start(watermark, window_to):
	"""Watermark minus the overlap, or a cold start. An unset Datetime field
	reads back as the zero date 0001-01-01, which arithmetic cannot survive."""
	cold_start = add_to_date(window_to, days=-ORPHAN_COLD_START_DAYS)
	try:
		stamp = get_datetime(watermark) if watermark else None
		if not stamp or stamp.year < 1900:
			return cold_start
		return min(add_to_date(stamp, hours=-ORPHAN_OVERLAP_HOURS), window_to)
	except Exception:
		return cold_start


def _ledger_paid(collection_name) -> float:
	"""What our immutable receipt spine says has been booked for this bill."""
	return flt(
		sum(
			frappe.get_all(
				"Payment Receipt Ledger",
				filters={"malipopay_collection": collection_name, "status": "Booked"},
				pluck="amount",
			)
			or [0]
		)
	)


def _stop_requested(log) -> bool:
	return bool(cint(frappe.db.get_value("Malipopay Sweep Log", log.name, "stop_requested")))


def _row(
	log,
	collection_name,
	*,
	action,
	doc=None,
	reference=None,
	ml_reference=None,
	provider_paid=0,
	ledger_paid=0,
	txns=0,
	amount=0,
	note="",
):
	"""Append one line to the divergence report. Best-effort, committed as it goes."""
	try:
		entry = frappe.get_doc("Malipopay Sweep Log", log.name)
		entry.append(
			"rows",
			{
				"reference": cstr(
					reference if reference is not None else (doc.control_number if doc else "")
				)[:140],
				"ml_reference": cstr(
					ml_reference if ml_reference is not None else (doc.ml_reference if doc else "")
				)[:140],
				"malipopay_collection": collection_name,
				"action": action,
				"txns_booked": cint(txns),
				"amount_booked": flt(amount),
				"provider_paid": flt(provider_paid),
				"ledger_paid": flt(ledger_paid),
				"note": cstr(note)[:500],
			},
		)
		entry.flags.ignore_permissions = True
		entry.save()
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Malipopay sweep: log row failed")


def _finish(log, status, message, counts, t_start, *, advance: bool, window_to=None) -> dict[str, Any]:
	try:
		frappe.db.set_value(
			"Malipopay Sweep Log",
			log.name,
			{
				"status": status,
				"ended_at": now(),
				"collections_seen": cint(counts.seen),
				"transactions_booked": cint(counts.booked),
				"amount_booked": flt(counts.amount),
				"orphans_adopted": cint(counts.orphans),
				"divergences_flagged": cint(counts.divergences),
				"summary": cstr(message)[:500],
			},
			update_modified=True,
		)
		if advance and window_to:
			# Only a completed run may move the watermark; anything else leaves
			# the window open so the next run re-covers it.
			frappe.db.set_single_value("Malipopay Settings", "sweep_watermark", window_to)
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Malipopay sweep: finish failed")
	return {
		"ok": status in ("Completed", "Stopped"),
		"status": status,
		"log": log.name,
		"message": message,
		"collections_seen": counts.seen,
		"booked": counts.booked,
		"amount": counts.amount,
		"orphans": counts.orphans,
		"divergences": counts.divergences,
		"errors": counts.errors,
		"duration": round(time.time() - t_start, 2),
	}
