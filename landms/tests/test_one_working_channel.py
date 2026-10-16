"""One working payment channel is enough to sell; the other is retried.

Operator rule, 14 Oct 2026: on a project that collects through TCB and
Malipopay, an order goes through when either can take payment on its number;
the one that failed is retried every hour with the same number. A number TCB
never confirmed does not hold a cancel while TCB is down — TCB is told later.
On a TCB-only project (production today) nothing changes. Banks are faked;
every write rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import malipopay, sales_order_hooks, tcb
from landms.payments import router

LIVE_TCB = frappe._dict(enabled=1, outbound_mode="Live", decline_reference_on_so_cancel=1)
LIVE_MALIPOPAY = frappe._dict(
	enabled=1, outbound_mode="Live", inbound_mode="Apply Payment", api_token="test-token"
)
TCB_DOWN = {"ok": False, "status": "Failed", "message": "HTTP 500", "unreachable": True}
TCB_SAYS_NO = {
	"ok": False,
	"status": "Failed",
	"message": "TCB Status 1: failed to validate partner",
	"unreachable": False,
}


class _Channel:
	"""A stand-in issuer: answers register/decline as told, records calls."""

	key = "TCB"

	def __init__(self, register=None, decline=None, never_registered=False, paid=False):
		self.register, self.decline, self.unconfirmed, self.paid = register, decline, never_registered, paid
		self.calls = []

	def register_reference(self, sales_order, reference):
		self.calls.append(("register", reference))
		return self.register

	def decline_reference(self, sales_order, reference):
		self.calls.append(("decline", reference))
		return self.decline

	def never_registered(self, reference):
		return self.unconfirmed

	def payment_ever_arrived(self, reference):
		return self.paid


class TestOneWorkingChannel(FrappeTestCase):
	def setUp(self):
		p = patch.object(frappe.db, "commit", lambda *a, **k: None)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)

	def _patch(self, target, **kwargs):
		p = patch(target, **kwargs)
		mocked = p.start()
		self.addCleanup(p.stop)
		return mocked

	def _issuer(self, channel):
		self._patch("landms.payments.router._channel_for_sales_order", return_value=channel)
		self._patch("landms.payments.registry.channel_for_reference", return_value=None)
		return channel

	# ------------------------------------------------------------------ submit

	def test_tcb_working_is_used_as_today(self):
		self._issuer(_Channel(register={"ok": True, "status": "Success"}))
		malipopay_asked = self._patch("landms.malipopay.can_take_payment_now")
		self.assertEqual(router.register_reference_on_submit("SO-T", "CN-T")["status"], "Success")
		malipopay_asked.assert_not_called()

	def test_tcb_down_and_malipopay_up_lets_the_order_through(self):
		self._issuer(_Channel(register=TCB_DOWN))
		self._patch("landms.malipopay.can_take_payment_now", return_value=True)
		result = router.register_reference_on_submit("SO-T", "CN-T")
		self.assertTrue(result["ok"])
		self.assertEqual((result["status"], result["via"]), ("Issuer Pending", "Malipopay"))

	def test_tcb_down_on_a_tcb_only_project_blocks_as_today(self):
		self._issuer(_Channel(register=TCB_DOWN))
		self._patch("landms.malipopay.collects_via_malipopay", return_value=False)
		self.assertEqual(router.register_reference_on_submit("SO-T", "CN-T"), TCB_DOWN)

	def test_both_down_blocks(self):
		self._issuer(_Channel(register=TCB_DOWN))
		self._patch("landms.malipopay.can_take_payment_now", return_value=False)
		self.assertFalse(router.register_reference_on_submit("SO-T", "CN-T")["ok"])

	def test_a_broken_fallback_never_lets_an_order_through(self):
		self._issuer(_Channel(register=TCB_DOWN))
		self._patch("landms.malipopay.can_take_payment_now", side_effect=RuntimeError("boom"))
		self._patch("frappe.log_error")
		self.assertFalse(router.register_reference_on_submit("SO-T", "CN-T")["ok"])

	def test_submit_gate_passes_and_flags_the_order_when_tcb_is_pending(self):
		self._patch(
			"landms.sales_order_hooks.register_reference_on_submit",
			return_value={"ok": True, "status": "Issuer Pending", "via": "Malipopay"},
		)
		doc = frappe._dict(name="SO-T", flags=frappe._dict())
		sales_order_hooks._register_with_tcb(doc, "CN-T")
		self.assertTrue(doc.flags.landms_issuer_pending)

	def test_submit_gate_still_blocks_when_nothing_works(self):
		self._patch("landms.sales_order_hooks.register_reference_on_submit", return_value=TCB_DOWN)
		with self.assertRaisesRegex(frappe.ValidationError, "could not be submitted"):
			sales_order_hooks._register_with_tcb(frappe._dict(name="SO-T", flags=frappe._dict()), "CN-T")

	# ------------------------------------------------------------------ can Malipopay take payment now?

	def test_malipopay_answers_only_on_a_live_malipopay_project(self):
		self._patch("landms.malipopay.collects_via_malipopay", return_value=True)
		self._patch("landms.malipopay.get_malipopay_settings", return_value=LIVE_MALIPOPAY)
		query = self._patch(
			"landms.malipopay.query_by_customer_reference", return_value=frappe._dict(all_ok=True, docs=[])
		)
		self.assertTrue(malipopay.can_take_payment_now("SO-T", "CN-T"))
		query.return_value = frappe._dict(all_ok=False, docs=[])
		self.assertFalse(malipopay.can_take_payment_now("SO-T", "CN-T"))

	def test_malipopay_check_runs_the_real_lookup(self):
		# the lookup itself is not faked here — only Malipopay's answer — so a
		# mistake in how the check calls it shows up (it did, 14 Oct: a crash
		# that safely read as "unavailable" and kept the order in Draft)
		class Answer:
			ok, status_code, text = True, 200, ""
			headers = {"content-type": "application/json"}

			def json(self):
				return {"success": True, "data": {"docs": []}}

		class Session:
			def get(self, *args, **kwargs):
				return Answer()

		self._patch("landms.malipopay.collects_via_malipopay", return_value=True)
		self._patch(
			"landms.malipopay.get_malipopay_settings",
			return_value=frappe._dict(
				LIVE_MALIPOPAY,
				base_url="https://malipopay.test",
				connect_timeout=1,
				read_timeout=1,
				verify_ssl=1,
			),
		)
		self._patch("landms.malipopay.get_request_session", return_value=Session())
		self._patch("landms.malipopay.create_malipopay_api_log")
		failures = self._patch("frappe.log_error")
		self.assertTrue(malipopay.can_take_payment_now("SO-T", "999101TEST0001"))
		failures.assert_not_called()

	def test_malipopay_switched_off_cannot_take_payment(self):
		self._patch("landms.malipopay.collects_via_malipopay", return_value=True)
		self._patch(
			"landms.malipopay.get_malipopay_settings",
			return_value=frappe._dict(LIVE_MALIPOPAY, outbound_mode="Log Only"),
		)
		query = self._patch("landms.malipopay.query_by_customer_reference")
		self.assertFalse(malipopay.can_take_payment_now("SO-T", "CN-T"))
		query.assert_not_called()

	# ------------------------------------------------------------------ cancel while TCB is down

	def _cancel(self, channel, malipopay_project=True, sold_while_tcb_down=True):
		self._issuer(channel)
		self._patch("landms.malipopay.collects_via_malipopay", return_value=malipopay_project)
		self._patch("landms.payments.router._sold_while_issuer_down", return_value=sold_while_tcb_down)
		self._patch("landms.payments.tcb_catch_up.add")
		followers = self._patch("landms.payments.router._followers_decline")
		return router.decline_reference_for_sales_order("SO-T", "CN-T"), followers

	def test_cancel_goes_ahead_when_all_four_conditions_hold(self):
		result, followers = self._cancel(_Channel(decline=TCB_DOWN, never_registered=True))
		self.assertEqual((result["ok"], result["status"]), (True, "Decline Deferred"))
		followers.assert_called_once()

	def test_cancel_still_waits_for_tcb_on_a_number_it_confirmed(self):
		result, followers = self._cancel(_Channel(decline=TCB_DOWN, never_registered=False))
		self.assertFalse(result["ok"])
		followers.assert_not_called()

	def test_a_clear_refusal_from_tcb_still_blocks(self):
		result, followers = self._cancel(_Channel(decline=TCB_SAYS_NO, never_registered=True))
		self.assertFalse(result["ok"])
		followers.assert_not_called()

	def test_tcb_only_projects_behave_exactly_as_before(self):
		result, followers = self._cancel(
			_Channel(decline=TCB_DOWN, never_registered=True), malipopay_project=False
		)
		self.assertFalse(result["ok"])
		followers.assert_not_called()

	def test_an_old_number_not_sold_while_tcb_was_down_still_waits_for_tcb(self):
		# numbers from before the Malipopay go-live have no Register step queued
		result, followers = self._cancel(
			_Channel(decline=TCB_DOWN, never_registered=True), sold_while_tcb_down=False
		)
		self.assertFalse(result["ok"])
		followers.assert_not_called()

	def test_sold_while_tcb_down_reads_the_waiting_for_tcb_queue(self):
		from landms.payments.tcb_catch_up import QUEUE

		if not frappe.db.table_exists(QUEUE):
			self.skipTest("Waiting for TCB is not installed on this site")
		number = "CN-TEST-SOLD-WHILE-DOWN"
		self.assertFalse(router._sold_while_issuer_down(number))
		entry = frappe.get_doc(
			{"doctype": QUEUE, "action": "Register", "status": "Done", "control_number": number}
		)
		entry.flags.ignore_links = True
		entry.insert(ignore_permissions=True, ignore_mandatory=True)
		self.addCleanup(frappe.db.rollback)
		self.assertTrue(router._sold_while_issuer_down(number))

	def test_a_number_that_ever_received_money_still_waits_for_tcb(self):
		result, followers = self._cancel(_Channel(decline=TCB_DOWN, never_registered=True, paid=True))
		self.assertFalse(result["ok"])
		followers.assert_not_called()

	def test_payment_ever_arrived_reads_notices_and_recorded_payments(self):
		so = frappe.db.get_value("Sales Order", {"docstatus": 1, "plot": ["is", "set"]}, "name")
		if not so:
			self.skipTest("this site has no submitted plot Sales Order")
		number = tcb.generate_control_number(so)
		while tcb.payment_ever_arrived(number):  # the site's copied TCB records already name some numbers
			number = tcb.generate_control_number(so)
		tcb.create_or_get_registry(control_number=number, sales_order=so)
		self.assertFalse(tcb.payment_ever_arrived(number))
		notice = frappe.get_doc({"doctype": "TCB Payment Notification", "reference": number, "amount": 100})
		notice.flags.ignore_mandatory = True
		notice.flags.ignore_links = True
		notice.insert(ignore_permissions=True)
		self.assertTrue(tcb.payment_ever_arrived(number))

	def test_decline_failures_say_whether_tcb_could_be_reached(self):
		source = __import__("inspect").getsource(tcb.decline_reference_for_sales_order)
		self.assertEqual(source.count('"unreachable"'), 2)

	def test_never_registered_reads_the_registry(self):
		so = frappe.db.get_value("Sales Order", {"docstatus": 1, "plot": ["is", "set"]}, "name")
		if not so:
			self.skipTest("this site has no submitted plot Sales Order")
		number = tcb.generate_control_number(so)
		tcb.create_or_get_registry(control_number=number, sales_order=so)
		self.assertTrue(tcb.never_registered(number))
		frappe.get_doc("TCB Control Number", number).append_log(None, "Reference Create", "Success")
		self.assertFalse(tcb.never_registered(number))

	# ------------------------------------------------------------------ what staff are told

	def test_malipopay_in_log_only_mode_cannot_take_payment(self):
		self._patch("landms.malipopay.collects_via_malipopay", return_value=True)
		self._patch(
			"landms.malipopay.get_malipopay_settings",
			return_value=frappe._dict(LIVE_MALIPOPAY, inbound_mode="Log Only"),
		)
		query = self._patch("landms.malipopay.query_by_customer_reference")
		self.assertFalse(malipopay.can_take_payment_now("SO-T", "CN-T"))
		query.assert_not_called()

	def _order_with_bill(self, number_status, bill_state):
		so = frappe.db.get_value("Sales Order", {"docstatus": 1, "plot": ["is", "set"]}, "name")
		if not so:
			self.skipTest("this site has no submitted plot Sales Order")
		number = tcb.generate_control_number(so)
		tcb.create_or_get_registry(control_number=number, sales_order=so)
		frappe.db.set_value("TCB Control Number", number, "status", number_status)
		frappe.db.set_value("Sales Order", so, {"control_number": number, "status": "To Deliver and Bill"})
		bill = frappe.get_doc(
			{
				"doctype": "Malipopay Collection",
				"control_number": number,
				"sales_order": so,
				"state": bill_state,
			}
		)
		bill.flags.ignore_links = True
		bill.flags.ignore_mandatory = True
		bill.insert(ignore_permissions=True)
		self._patch("landms.malipopay.collects_via_malipopay", return_value=True)
		self._patch("landms.malipopay.get_malipopay_settings", return_value=LIVE_MALIPOPAY)
		self._patch("landms.tcb._get_tcb_settings", return_value=LIVE_TCB)
		return so, number

	def test_order_sold_while_tcb_was_down_warns_staff(self):
		so, number = self._order_with_bill("Failed", "Active")
		from landms.payments import tcb_catch_up

		if not frappe.db.table_exists(tcb_catch_up.QUEUE):
			self.skipTest("the Waiting for TCB table is not on this site yet")
		entry = tcb_catch_up.add("Register", so, number, "test")
		notice = malipopay.sales_order_malipopay_notice(so)
		self.assertIn("Not set up at TCB yet", notice["message"])
		self.assertEqual(notice["waiting_for_tcb"], entry)

	def test_no_warning_once_tcb_has_the_number(self):
		so, _number = self._order_with_bill("Registered", "Active")
		self.assertEqual(malipopay.sales_order_malipopay_notice(so), {})

	def test_a_failed_bill_with_tcb_not_set_up_says_the_customer_cannot_pay(self):
		so, _number = self._order_with_bill("Failed", "Create Failed")
		notice = malipopay.sales_order_malipopay_notice(so)
		self.assertIn("cannot pay at all yet", notice["message"])
		self.assertEqual(notice["indicator"], "red")

	def test_a_failed_bill_with_tcb_working_says_pay_through_tcb(self):
		so, _number = self._order_with_bill("Registered", "Create Failed")
		self.assertIn("can only pay through TCB", malipopay.sales_order_malipopay_notice(so)["message"])
