# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Waiting for TCB — the TCB steps that could not be done while TCB was down,
kept in a queue so the sales team is not stuck when nobody is around.

Only two moments add an entry, both new with the one-working-rail rule
(operator, 14 Oct 2026), so data from before the Malipopay go-live can never
get in:
  * Register — an order was sold through Malipopay because TCB was down
    (sales_order_hooks.submit_sales_order);
  * Decline — a cancel / close / terminate went ahead while TCB was down for a
    number TCB had never confirmed (payments.router.decline_reference_for_sales_order).

Right before any call the entry is checked again and dropped, with no call,
if anything changed: the order stopped or reopened, it no longer uses the
number, the number was switched off on purpose, the order is fully paid, or
its Malipopay bill was ended. An entry still not done after three days goes to
a person. The automatic pass runs every three hours (never at midnight) and
only when TCB Integration Settings "Automatic Catch-up" is ticked; the Try now
button works whenever TCB is Live. Nothing here ever looks at a number that
is not in this queue.
"""

import frappe
from frappe.utils import add_days, cint, cstr, flt, get_datetime, now, now_datetime

QUEUE = "Waiting for TCB"
GIVE_UP_AFTER_DAYS = 3
# Malipopay bill states that mean someone stopped it (ended or paused)
ENDED_BILL_STATES = ("Kill Requested", "Dead", "Suspend Requested", "Suspended")
ROLES = ("System Manager", "Accounts Manager", "Sales Manager", "LandMS Sales Manager")


def add(action, sales_order, control_number, reason):
	"""Put a TCB step in the queue. Never throws: a sale or a stop must not
	fail because of the queue. A Decline also drops a waiting Register for the
	same number — the order it was for has been stopped."""
	try:
		if not control_number or not frappe.db.table_exists(QUEUE):
			return None
		if action == "Decline":
			for name in frappe.get_all(
				QUEUE,
				filters={"control_number": control_number, "action": "Register", "status": "Waiting"},
				pluck="name",
			):
				_close(name, "Dropped", "The order was stopped, so the number is not set up at TCB.")
		existing = frappe.db.get_value(
			QUEUE, {"control_number": control_number, "action": action, "status": "Waiting"}, "name"
		)
		if existing:
			return existing
		doc = frappe.get_doc(
			{
				"doctype": QUEUE,
				"action": action,
				"status": "Waiting",
				"control_number": control_number,
				"sales_order": sales_order,
				"customer": frappe.db.get_value("Sales Order", sales_order, "customer")
				if sales_order
				else None,
				"reason": cstr(reason)[:500],
				"queued_by": frappe.session.user,
				"queued_at": now(),
			}
		)
		doc.flags.ignore_links = True
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(
			frappe.get_traceback(), f"Waiting for TCB: could not queue {action} {control_number}"
		)
		return None


def process_queue(limit=20):
	"""Scheduler, every three hours (hooks.py). The first step is the one
	query for waiting entries; with none the run ends there. Stops at the first
	failure — TCB is most likely still down — and a failed entry moves to the
	back of the queue."""
	if not frappe.db.table_exists(QUEUE):
		return
	names = frappe.get_all(
		QUEUE, filters={"status": "Waiting"}, order_by="modified asc", limit=limit, pluck="name"
	)
	if not names:
		return
	from landms.tcb import _get_tcb_settings

	settings = _get_tcb_settings()
	if not cint(settings.get("auto_catch_up")):
		# switched off: nothing is sent to TCB, but a step left waiting too long
		# is still handed to a person (Needs a Person + bell), so none sits unseen
		for name in names:
			_hand_to_a_person_if_overdue(frappe.get_doc(QUEUE, name))
		return
	for name in names:
		if run_entry(name, by="automatic catch-up") == "failed":
			return


def run_entry(name, by, manual=False):
	"""Check one entry again and, if still needed, do it at TCB. Returns done,
	dropped, needs_person, failed or skipped. Owns its commit."""
	doc = frappe.get_doc(QUEUE, name)
	allowed = ("Waiting", "Needs a Person") if manual else ("Waiting",)
	if doc.status not in allowed:
		return "skipped"
	if not manual and _hand_to_a_person_if_overdue(doc):
		return "needs_person"

	blocker = _tcb_not_ready(doc)
	if blocker:
		_record(name, blocker, attempted=False)
		frappe.db.commit()
		return "failed"

	check = _check_again(doc)
	if check == "done":
		_close(name, "Done", f"TCB already has it ({_registry_status(doc.control_number)}).")
		frappe.db.commit()
		return "done"
	if check:
		_close(name, "Dropped", check)
		frappe.db.commit()
		return "dropped"

	error = None
	try:
		result = _call_tcb(doc)
	except Exception:
		frappe.db.rollback()
		error = frappe.get_traceback()
		result = {"ok": False, "message": "the call raised an error (see Error Log)"}
	ok = bool(result.get("ok"))
	_record(
		name,
		"{}: {}".format(result.get("status") or ("OK" if ok else "Failed"), result.get("message") or ""),
	)
	if ok:
		_close(name, "Done", f"Done by {by}.")
	if error:
		frappe.log_error(error, f"Waiting for TCB: {doc.action} {doc.control_number} failed")
	frappe.db.commit()
	return "done" if ok else "failed"


def _hand_to_a_person_if_overdue(doc):
	"""A Waiting step older than GIVE_UP_AFTER_DAYS goes to Needs a Person with
	a bell notification. No call to TCB. Returns True when it was handed over."""
	if doc.status != "Waiting":
		return False
	if get_datetime(doc.queued_at) >= add_days(now_datetime(), -GIVE_UP_AFTER_DAYS):
		return False
	note = f"Still not done after {GIVE_UP_AFTER_DAYS} days — please check it and press Try now, or Don't do this."
	_close(doc.name, "Needs a Person", note)
	_notify(doc, f"{doc.action} at TCB for {doc.control_number} needs a person: {note}")
	frappe.db.commit()
	return True


def _tcb_not_ready(doc):
	"""Why TCB cannot be called right now, or None."""
	from landms.tcb import _get_tcb_settings

	settings = _get_tcb_settings()
	if not cint(settings.get("enabled")) or settings.get("outbound_mode") != "Live":
		return "TCB integration is not Live — nothing sent."
	if doc.action == "Decline" and not cint(settings.get("decline_reference_on_so_cancel")):
		return "TCB declines are switched off (TCB Integration Settings) — nothing sent."
	return None


def _check_again(doc):
	"""Right before calling TCB: None to go ahead, "done" if TCB already has
	it, or the reason the step is no longer wanted."""
	so = (
		frappe.db.get_value(
			"Sales Order",
			doc.sales_order,
			["docstatus", "status", "control_number", "plot_sales_invoice"],
			as_dict=True,
		)
		if doc.sales_order
		else None
	)
	registry = _registry_status(doc.control_number)
	if not so:
		return "The order no longer exists."
	if doc.action == "Register":
		if so.docstatus != 1 or so.status in ("Closed", "Completed", "On Hold"):
			return "The order is no longer open ({}).".format("Cancelled" if so.docstatus == 2 else so.status)
		if cstr(so.control_number).strip() != doc.control_number:
			return "The order no longer uses this number."
		if registry in ("Registered", "Paid"):
			return "done"
		if registry in ("Declined", "Expired"):
			return "The number was switched off at TCB on purpose."
		if _fully_paid(so.plot_sales_invoice):
			return "The order is fully paid — TCB is not needed."
		if _malipopay_bill_ended(doc.control_number):
			return "The Malipopay bill was ended or paused, so the number is not set up at TCB either."
		return None
	# Decline: only for an order that is still stopped
	if not (so.docstatus == 2 or so.status == "Closed"):
		return "The order is open again — its number is not switched off."
	if registry == "Declined":
		return "done"
	return None


def _call_tcb(doc):
	if doc.action == "Register":
		from landms.payments.router import register_reference_for_sales_order

		return register_reference_for_sales_order(doc.sales_order, doc.control_number)
	from landms.payments.registry import channel_for_reference
	from landms.payments.router import _channel_for_sales_order

	channel = channel_for_reference(doc.control_number) or _channel_for_sales_order(doc.sales_order)
	return channel.decline_reference(doc.sales_order, doc.control_number)


def _registry_status(control_number):
	return frappe.db.get_value("TCB Control Number", control_number, "status") if control_number else None


def _fully_paid(invoice_name):
	if not invoice_name:
		return False
	inv = frappe.db.get_value(
		"Sales Invoice", invoice_name, ["docstatus", "outstanding_amount"], as_dict=True
	)
	return bool(inv and inv.docstatus == 1 and flt(inv.outstanding_amount) <= 0)


def _malipopay_bill_ended(control_number):
	"""True when this number's Malipopay bill was stopped on purpose (ended or
	paused) and none is live. No bill at all is not "ended": TCB is then the
	only way to pay and is needed even more."""
	if not frappe.db.table_exists("Malipopay Collection"):
		return False
	states = frappe.get_all("Malipopay Collection", filters={"control_number": control_number}, pluck="state")
	return bool(states) and all(state in ENDED_BILL_STATES for state in states)


def _record(name, message, attempted=True):
	values = {"last_result": cstr(message)[:500], "last_attempt_at": now()}
	if attempted:
		values["attempts"] = cint(frappe.db.get_value(QUEUE, name, "attempts")) + 1
	frappe.db.set_value(QUEUE, name, values)  # updates modified: a failed entry moves to the back


def _close(name, status, note):
	frappe.db.set_value(QUEUE, name, {"status": status, "closed_at": now(), "closed_note": cstr(note)[:500]})


def _notify(doc, subject):
	"""Bell notification to the order's owner and whoever added the entry."""
	try:
		owner = frappe.db.get_value("Sales Order", doc.sales_order, "owner") if doc.sales_order else None
		for user in {owner, doc.queued_by} - {None, "", "Guest"}:
			frappe.get_doc(
				{
					"doctype": "Notification Log",
					"for_user": user,
					"type": "Alert",
					"document_type": QUEUE,
					"document_name": doc.name,
					"subject": subject[:140],
				}
			).insert(ignore_permissions=True)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Waiting for TCB: notification failed " + cstr(doc.name))
