"""Waiting for TCB — the queue of TCB steps that could not be done while TCB
was down (operator, 14 Oct 2026: it must work when nobody is around, and must
never touch data from before the Malipopay go-live).

The first test runs against the site's real data WITHOUT clearing anything:
numbers that were never registered already exist, and the queue must leave
every one of them alone. TCB is faked throughout; every write rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, now

from landms import sales_order_hooks, tcb
from landms.payments import router, tcb_catch_up

QUEUE = tcb_catch_up.QUEUE
LIVE = frappe._dict(enabled=1, outbound_mode="Live", decline_reference_on_so_cancel=1, auto_catch_up=1)


class _Tcb:
	"""Stand-in for TCB: answers as told and records every call."""

	key = "TCB"

	def __init__(self, answer=None, never_registered=True):
		self.answer = answer or {"ok": True, "status": "Success"}
		self.unconfirmed = never_registered
		self.calls = []

	def register(self, sales_order, number):
		self.calls.append(("register", number))
		return self.answer

	def register_reference(self, sales_order, number):
		return self.register(sales_order, number)

	def decline_reference(self, sales_order, number):
		self.calls.append(("decline", number))
		return self.answer

	def never_registered(self, number):
		return self.unconfirmed


class TestWaitingForTcb(FrappeTestCase):
	def setUp(self):
		if not frappe.db.table_exists(QUEUE):
			self.skipTest("the Waiting for TCB table is not on this site yet")
		p = patch.object(frappe.db, "commit", lambda *a, **k: None)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)
		self.tcb = _Tcb()
		self.settings = self._patch("landms.tcb._get_tcb_settings", return_value=LIVE)
		self._patch(
			"landms.payments.router.register_reference_for_sales_order", side_effect=self.tcb.register
		)
		self._patch("landms.payments.router._channel_for_sales_order", return_value=self.tcb)
		self._patch("landms.payments.registry.channel_for_reference", return_value=None)

	def _patch(self, target, **kwargs):
		p = patch(target, **kwargs)
		mocked = p.start()
		self.addCleanup(p.stop)
		return mocked

	def _open_order(self, number_status="Failed", skip=()):
		"""A submitted, open plot order whose own number is still unregistered."""
		so = frappe.db.get_value(
			"Sales Order",
			{"docstatus": 1, "plot": ["is", "set"], "name": ["not in", list(skip) or [""]]},
			"name",
		)
		if not so:
			self.skipTest("this site has no submitted plot Sales Order")
		number = tcb.generate_control_number(so)
		tcb.create_or_get_registry(control_number=number, sales_order=so)
		frappe.db.set_value("TCB Control Number", number, "status", number_status)
		frappe.db.set_value("Sales Order", so, {"control_number": number, "status": "To Deliver and Bill"})
		invoice = frappe.db.get_value("Sales Order", so, "plot_sales_invoice")
		if invoice:
			frappe.db.set_value("Sales Invoice", invoice, "outstanding_amount", 1000)
		return so, number

	def _row(self, action, so, number):
		return tcb_catch_up.add(action, so, number, "test")

	def _status(self, name):
		return frappe.db.get_value(QUEUE, name, "status")

	# ------------------------------------------------------------------ old data is never touched

	def test_old_unregistered_numbers_are_never_touched(self):
		before = {
			r.name: r.status
			for r in frappe.get_all(
				"TCB Control Number",
				filters={"status": ["in", ["Generated", "Failed"]]},
				fields=["name", "status"],
			)
		}
		tcb_catch_up.process_queue()
		after = {
			r.name: r.status
			for r in frappe.get_all(
				"TCB Control Number",
				filters={"name": ["in", list(before) or [""]]},
				fields=["name", "status"],
			)
		}
		self.assertEqual(self.tcb.calls, [])
		self.assertEqual(before, after)

	def test_an_old_open_order_with_an_unregistered_number_is_left_alone(self):
		# exactly what the removed retry job registered by itself on 14 Oct
		so, number = self._open_order(number_status="Failed")
		stopped_so, stopped_number = self._open_order(number_status="Generated", skip=[so])
		frappe.db.set_value("Sales Order", stopped_so, "docstatus", 2)
		tcb_catch_up.process_queue()
		self.assertEqual(self.tcb.calls, [])
		self.assertEqual(frappe.db.get_value("TCB Control Number", number, "status"), "Failed")
		self.assertEqual(frappe.db.get_value("TCB Control Number", stopped_number, "status"), "Generated")

	# ------------------------------------------------------------------ what adds a step

	def test_a_sale_through_malipopay_queues_registering_at_tcb(self):
		so, number = self._open_order()
		doc = frappe.get_doc("Sales Order", so)
		doc.flags.landms_issuer_pending = {"ok": True, "status": "Issuer Pending"}
		for name in (
			"_link_application_to_sales_order",
			"_ensure_draft_plot_contract",
			"_mark_plot_pending_advance",
		):
			self._patch("landms.sales_order_hooks." + name, return_value=None)
		sales_order_hooks.submit_sales_order(doc)
		self.assertEqual(
			frappe.db.get_value(QUEUE, {"control_number": number}, ["action", "status"]),
			("Register", "Waiting"),
		)

	def test_a_stop_while_tcb_is_down_queues_switching_off_and_drops_the_register(self):
		so, number = self._open_order()
		register = self._row("Register", so, number)
		self.tcb.answer = {"ok": False, "status": "Failed", "message": "TCB down", "unreachable": True}
		self.tcb.payment_ever_arrived = lambda number: False
		self._patch("landms.malipopay.collects_via_malipopay", return_value=True)
		self._patch("landms.payments.router._followers_decline")
		result = router.decline_reference_for_sales_order(so, number)
		self.assertEqual(result["status"], "Decline Deferred")
		self.assertEqual(
			frappe.db.get_value(QUEUE, {"control_number": number, "action": "Decline"}, "status"), "Waiting"
		)
		self.assertEqual(self._status(register), "Dropped")

	def test_queueing_never_breaks_a_sale(self):
		with patch.object(frappe.db, "table_exists", return_value=False):
			self.assertIsNone(tcb_catch_up.add("Register", "SO-X", "CN-X", "test"))

	# ------------------------------------------------------------------ doing a step

	def test_a_waiting_register_is_done_at_tcb(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		tcb_catch_up.process_queue()
		self.assertEqual(self.tcb.calls, [("register", number)])
		self.assertEqual(self._status(row), "Done")

	def test_a_waiting_decline_is_done_at_tcb_for_a_stopped_order(self):
		so, number = self._open_order()
		row = self._row("Decline", so, number)
		frappe.db.set_value("Sales Order", so, "status", "Closed")
		tcb_catch_up.process_queue()
		self.assertEqual(self.tcb.calls, [("decline", number)])
		self.assertEqual(self._status(row), "Done")

	# ------------------------------------------------------------------ checked again, dropped with no call

	def _dropped_without_a_call(self, row):
		tcb_catch_up.process_queue()
		self.assertEqual(self.tcb.calls, [])
		self.assertEqual(self._status(row), "Dropped")

	def test_register_dropped_when_the_order_was_cancelled(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		frappe.db.set_value("Sales Order", so, "docstatus", 2)
		self._dropped_without_a_call(row)

	def test_register_dropped_when_the_number_was_switched_off_on_purpose(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		frappe.db.set_value("TCB Control Number", number, "status", "Declined")
		self._dropped_without_a_call(row)

	def test_register_dropped_when_the_order_uses_another_number(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		frappe.db.set_value("Sales Order", so, "control_number", "999100TESTOTHER")
		self._dropped_without_a_call(row)

	def test_register_dropped_when_the_order_is_fully_paid(self):
		so, number = self._open_order()
		invoice = frappe.db.get_value("Sales Order", so, "plot_sales_invoice")
		if not invoice:
			self.skipTest("the chosen order has no plot invoice")
		row = self._row("Register", so, number)
		frappe.db.set_value("Sales Invoice", invoice, "outstanding_amount", 0)
		self._dropped_without_a_call(row)

	def test_register_dropped_when_the_malipopay_bill_was_ended(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		bill = frappe.get_doc(
			{"doctype": "Malipopay Collection", "control_number": number, "sales_order": so, "state": "Dead"}
		)
		bill.flags.ignore_links = True
		bill.flags.ignore_mandatory = True
		bill.insert(ignore_permissions=True)
		self._dropped_without_a_call(row)

	def test_decline_dropped_when_the_order_is_open_again(self):
		so, number = self._open_order()
		row = self._row("Decline", so, number)  # order left open
		self._dropped_without_a_call(row)

	def test_already_registered_is_done_without_a_call(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		frappe.db.set_value("TCB Control Number", number, "status", "Registered")
		tcb_catch_up.process_queue()
		self.assertEqual(self.tcb.calls, [])
		self.assertEqual(self._status(row), "Done")

	# ------------------------------------------------------------------ gentle on the workers, never forever

	def test_nothing_is_sent_while_the_switch_is_off(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		self.settings.return_value = frappe._dict(LIVE, auto_catch_up=0)
		tcb_catch_up.process_queue()
		self.assertEqual(self.tcb.calls, [])
		self.assertEqual(self._status(row), "Waiting")

	def test_an_empty_queue_ends_the_run_before_reading_settings(self):
		frappe.db.sql("update `tabWaiting for TCB` set status = 'Done' where status = 'Waiting'")
		tcb_catch_up.process_queue()
		self.settings.assert_not_called()

	def test_the_run_stops_at_the_first_failure_and_records_it(self):
		first_so, first = self._open_order()
		second_so, second = self._open_order(skip=[first_so])
		first_row = self._row("Register", first_so, first)
		self._row("Register", second_so, second)
		self.tcb.answer = {"ok": False, "status": "Failed", "message": "HTTP 500"}
		tcb_catch_up.process_queue()
		self.assertEqual(len(self.tcb.calls), 1)
		row = frappe.db.get_value(QUEUE, first_row, ["status", "attempts", "last_result"], as_dict=True)
		self.assertEqual((row.status, row.attempts), ("Waiting", 1))
		self.assertIn("HTTP 500", row.last_result)

	def test_after_three_days_it_goes_to_a_person_not_to_tcb(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		frappe.db.set_value(QUEUE, row, "queued_at", add_days(now(), -4))
		tcb_catch_up.process_queue()
		self.assertEqual(self.tcb.calls, [])
		self.assertEqual(self._status(row), "Needs a Person")

	# ------------------------------------------------------------------ the buttons

	def test_try_now_works_with_the_switch_off_and_on_a_step_for_a_person(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		frappe.db.set_value(QUEUE, row, "status", "Needs a Person")
		self.settings.return_value = frappe._dict(LIVE, auto_catch_up=0)
		result = frappe.get_doc(QUEUE, row).try_now()
		self.assertEqual(result["outcome"], "done")
		self.assertEqual(self.tcb.calls, [("register", number)])

	def test_try_now_sends_nothing_when_tcb_is_not_live(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		self.settings.return_value = frappe._dict(LIVE, outbound_mode="Off")
		self.assertEqual(frappe.get_doc(QUEUE, row).try_now()["outcome"], "failed")
		self.assertEqual(self.tcb.calls, [])

	def test_dont_do_this_closes_it_with_nothing_sent(self):
		so, number = self._open_order()
		row = self._row("Register", so, number)
		frappe.get_doc(QUEUE, row).stop(note="customer withdrew")
		self.assertEqual(self._status(row), "Dropped")
		self.assertEqual(self.tcb.calls, [])

	def test_only_managers_may_press_the_buttons(self):
		so, number = self._open_order()
		row = frappe.get_doc(QUEUE, self._row("Register", so, number))
		original = frappe.session.user
		self.addCleanup(lambda: frappe.set_user(original))
		frappe.set_user("Guest")
		with patch.dict(frappe.local.flags, {"in_test": False}), self.assertRaises(frappe.PermissionError):
			row.try_now()
