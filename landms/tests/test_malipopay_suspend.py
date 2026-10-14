"""Malipopay bill lifecycle on order stop (contract v1.2 §12.2).

A cancelled order (no money) expires its bill; a closed order holding money
suspends it; Reopen resumes it; "Close for good at Malipopay" expires it and
blocks Reopen. Every HTTP call, commit and enqueue is faked — nothing leaves
the machine and the whole test rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import malipopay, sales_order_hooks

SETTINGS = frappe._dict(
	enabled=1,
	outbound_mode="Live",
	api_token="test-token",
	base_url="https://malipopay.test",
	connect_timeout=1,
	read_timeout=1,
	verify_ssl=1,
	amount_type="FLEXIBLE",
)


class _Reply:
	def __init__(self, status_code, body):
		self.status_code = status_code
		self._body = body
		self.text = ""

	def json(self):
		return self._body


class _Session:
	"""Records every POST; answers with the queued replies (or raises)."""

	def __init__(self):
		self.calls = []
		self.replies = []

	def post(self, url, json=None, **kwargs):
		self.calls.append((url, json))
		reply = self.replies.pop(0) if self.replies else None
		if reply is None or isinstance(reply, Exception):
			raise reply or ConnectionError("no answer")
		return reply


class TestMalipopaySuspend(FrappeTestCase):
	def setUp(self):
		self.session = _Session()
		self.logs = []
		self.enqueued = []
		self.provider_status = None
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		frappe.flags.pop("mp_move_queued", None)
		frappe.flags.pop("mp_kill_queued", None)
		for target, kwargs in (
			("landms.malipopay.get_malipopay_settings", {"return_value": SETTINGS}),
			("landms.malipopay.get_request_session", {"return_value": self.session}),
			("landms.malipopay.create_malipopay_api_log", {"side_effect": lambda **kw: self.logs.append(kw)}),
			("landms.malipopay.book_receipts_for_collection", {"return_value": None}),
			(
				"landms.malipopay._provider_status",
				{"side_effect": lambda settings, doc: self.provider_status},
			),
			("frappe.enqueue", {"side_effect": lambda method, **kw: self.enqueued.append(method)}),
		):
			p = patch(target, **kwargs)
			p.start()
			self.addCleanup(p.stop)
		p = patch.object(frappe.db, "commit", lambda *a, **k: None)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(lambda: frappe.set_user(self._orig_user))

		# one order of each kind from the site's own data
		self.so_open_with_money = self._order(status=["not in", ["Closed", "Completed"]], with_money=True)
		self.so_open_no_money = self._order(status=["not in", ["Closed", "Completed"]], with_money=False)
		self.so_closed_with_money = self._order(status="Closed", forfeited=True)
		self.so_closed_no_money = self._order(status="Closed", forfeited=False)
		self._n = 0

	def _order(self, status, with_money=None, forfeited=None):
		filters = {"docstatus": 1, "status": status, "plot": ["is", "set"]}
		if forfeited is not None:
			filters["forfeiture_entry"] = ["is", "set"] if forfeited else ["is", "not set"]
		for name in frappe.get_all(
			"Sales Order", filters=filters, pluck="name", order_by="creation desc", limit=50
		):
			if malipopay._stopped_for_good(name):
				continue  # a real order someone closed for good is no longer of any kind the tests need
			if with_money is None or malipopay._order_holds_money(name) == with_money:
				return name
		self.skipTest(f"this site has no Sales Order of the kind the test needs ({filters})")

	def _bill(self, sales_order, state="Active", ml=True):
		self._n += 1
		doc = frappe.get_doc(
			{
				"doctype": "Malipopay Collection",
				"control_number": f"TESTMPSUSP{self._n}",
				"sales_order": sales_order,
				"state": state,
				"ml_reference": f"MLTEST{self._n}" if ml else None,
			}
		)
		doc.flags.ignore_links = True
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		return doc

	def _reload(self, doc):
		return frappe.get_doc("Malipopay Collection", doc.name)

	# ------------------------------------------------------------------ the requests we send

	def test_create_payload_sends_push_false(self):
		payload = malipopay._build_create_payload(
			frappe._dict(sales_order=self.so_open_with_money, control_number="X", amount=1000), SETTINGS
		)
		self.assertIs(payload["push"], False)
		self.assertNotIn("pushUSSD", payload)

	def test_status_call_uses_the_v12_address_and_body(self):
		bill = self._bill(self.so_open_no_money)
		self.session.replies.append(
			_Reply(200, {"success": True, "code": 3170, "data": {"status": "EXPIRED"}})
		)
		sent = malipopay._send_status(bill, SETTINGS, "EXPIRED", "Sales Order X cancelled in LandMS")
		url, body = self.session.calls[0]
		self.assertEqual(url, f"https://malipopay.test/api/v1/payment/{bill.ml_reference}/status")
		self.assertEqual(
			body,
			{
				"status": "EXPIRED",
				"reason": "Sales Order X cancelled in LandMS",
				"externalReference": self.so_open_no_money,
			},
		)
		self.assertTrue(sent.ok)
		self.assertEqual(self.logs[0]["event_type"], "Expire")

	# ------------------------------------------------------------------ what a stop asks for

	def test_decline_pauses_the_bill_of_an_order_holding_money(self):
		bill = self._bill(self.so_open_with_money)
		malipopay.follow_decline_for_sales_order(
			self.so_open_with_money, bill.control_number, "declined at TCB"
		)
		self.assertEqual(self._reload(bill).state, "Suspend Requested")
		self.assertIn("landms.malipopay.suspend_collection", self.enqueued)

	def test_decline_expires_the_bill_of_an_order_without_money(self):
		bill = self._bill(self.so_open_no_money)
		malipopay.follow_decline_for_sales_order(
			self.so_open_no_money, bill.control_number, "declined at TCB"
		)
		self.assertEqual(self._reload(bill).state, "Kill Requested")
		self.assertIn("landms.malipopay.expire_collection", self.enqueued)

	def test_a_bill_never_sent_to_malipopay_is_simply_stopped(self):
		bill = self._bill(self.so_open_with_money, state="Pending Create", ml=False)
		malipopay.follow_decline_for_sales_order(
			self.so_open_with_money, bill.control_number, "declined at TCB"
		)
		self.assertEqual(self._reload(bill).state, "Dead")

	# ------------------------------------------------------------------ termination is final

	def test_termination_expires_the_bill_even_with_money_on_it(self):
		bill = self._bill(self.so_open_with_money)
		malipopay.follow_decline_for_sales_order(
			self.so_open_with_money, bill.control_number, "declined at TCB", final=True
		)
		self.assertEqual(self._reload(bill).state, "Kill Requested")
		self.assertIn("landms.malipopay.expire_collection", self.enqueued)

	def test_router_tells_the_followers_whether_the_stop_is_final(self):
		from landms.payments import router

		with patch("landms.malipopay.follow_decline_for_sales_order") as follower:
			router._followers_decline("TCB", "SO-TEST", "CN-TEST", final=True)
			router._followers_decline("TCB", "SO-TEST", "CN-TEST")
		self.assertEqual([c.kwargs["final"] for c in follower.call_args_list], [True, False])

	def test_terminate_declines_the_number_as_final(self):
		row = frappe.db.sql("""select pc.name from `tabPlot Contract` pc
			join `tabSales Order` so on so.name = pc.sales_order
			where pc.docstatus = 1 and ifnull(so.control_number, '') != '' limit 1""")
		if not row:
			self.skipTest("this site has no submitted contract on an order with a payment number")
		contract = frappe.get_doc("Plot Contract", row[0][0])
		with (
			patch(
				"landms.sales_order_hooks.decline_reference_for_sales_order",
				return_value={"ok": False, "message": "test stop"},
			) as decline,
			self.assertRaises(frappe.ValidationError),
		):
			contract._decline_control_number_on_termination_or_block()
		self.assertTrue(decline.call_args.kwargs.get("final"))

	def test_hourly_pass_expires_the_paused_bill_of_a_terminated_contract(self):
		row = frappe.db.sql("""select pc.name, pc.sales_order from `tabPlot Contract` pc
			join `tabSales Order` so on so.name = pc.sales_order
			where pc.docstatus = 1 and so.docstatus = 1 limit 1""")
		if not row:
			self.skipTest("this site has no submitted contract")
		contract, so = row[0]
		frappe.db.set_value("Plot Contract", contract, "contract_status", "Terminated", update_modified=False)
		frappe.db.set_value("Sales Order", so, "status", "Closed", update_modified=False)
		bill = self._bill(so, state="Suspended")
		with (
			patch("landms.malipopay.expire_collection"),
			patch("landms.malipopay.suspend_collection"),
			patch("landms.malipopay.resume_collection"),
		):
			malipopay.kill_pending_collections(limit=500)
		bill = self._reload(bill)
		self.assertEqual(bill.state, "Kill Requested")
		self.assertIn("terminated", bill.last_event)

	# ------------------------------------------------------------------ the workers

	def test_suspend_lands(self):
		bill = self._bill(self.so_open_with_money, state="Suspend Requested")
		self.session.replies.append(
			_Reply(200, {"success": True, "code": 3170, "data": {"status": "SUSPENDED"}})
		)
		malipopay.suspend_collection(bill.name)
		bill = self._reload(bill)
		self.assertEqual(bill.state, "Suspended")
		self.assertEqual(bill.provider_status, "SUSPENDED")

	def test_suspend_refused_because_already_paid_ends_the_bill(self):
		bill = self._bill(self.so_open_with_money, state="Suspend Requested")
		self.provider_status = "PARTIAL"
		self.session.replies.append(_Reply(422, {"success": False, "code": 3174, "message": "already paid"}))
		malipopay.suspend_collection(bill.name)
		self.assertEqual(self._reload(bill).state, "Dead")

	def test_suspend_unanswered_stays_requested_and_flagged(self):
		bill = self._bill(self.so_open_with_money, state="Suspend Requested")
		self.session.replies.append(ConnectionError("timeout"))
		malipopay.suspend_collection(bill.name)
		bill = self._reload(bill)
		self.assertEqual(bill.state, "Suspend Requested")
		self.assertTrue(bill.needs_attention)
		self.assertTrue(bill.attention_reason.startswith(malipopay.SUSPEND_FLAG))

	def test_expire_refused_because_already_paid_ends_the_bill(self):
		bill = self._bill(self.so_open_no_money, state="Kill Requested")
		self.provider_status = "PROCESSING"
		self.session.replies.append(_Reply(422, {"success": False, "code": 3174, "message": "already paid"}))
		malipopay.expire_collection(bill.name)
		self.assertEqual(self._reload(bill).state, "Dead")

	# ------------------------------------------------------------------ the two ways out of a closed order

	def test_reopen_resumes_the_paused_bill(self):
		bill = self._bill(self.so_open_with_money, state="Suspended")
		frappe.db.set_value(
			"Sales Order",
			self.so_open_with_money,
			"control_number",
			bill.control_number,
			update_modified=False,
		)
		malipopay.reopen_collection_for_sales_order(self.so_open_with_money)
		self.assertEqual(self._reload(bill).state, "Resume Requested")
		self.assertIn("landms.malipopay.resume_collection", self.enqueued)
		self.session.replies.append(
			_Reply(200, {"success": True, "code": 3170, "data": {"status": "PARTIAL"}})
		)
		malipopay.resume_collection(bill.name)
		bill = self._reload(bill)
		self.assertEqual(bill.state, "Active")
		self.assertEqual(bill.provider_status, "PARTIAL")

	def test_close_for_good_expires_the_bill_and_blocks_reopen(self):
		bill = self._bill(self.so_closed_with_money, state="Suspended")
		frappe.db.set_value(
			"Sales Order",
			self.so_closed_with_money,
			"control_number",
			bill.control_number,
			update_modified=False,
		)
		malipopay.close_for_good_at_malipopay(self.so_closed_with_money)
		self.assertTrue(malipopay.is_closed_for_good(self.so_closed_with_money))
		self.assertEqual(self._reload(bill).state, "Kill Requested")
		self.assertIn("landms.malipopay.expire_collection", self.enqueued)
		with self.assertRaisesRegex(frappe.ValidationError, "closed for good"):
			sales_order_hooks.reopen_sales_order(self.so_closed_with_money)

	def test_erpnext_reopen_refused_after_close_for_good(self):
		frappe.db.set_value("Sales Order", self.so_closed_with_money, malipopay.CLOSED_FOR_GOOD_FIELD, 1)
		with self.assertRaisesRegex(frappe.ValidationError, "closed for good"):
			frappe.get_doc("Sales Order", self.so_closed_with_money).update_status("Draft")
		self.assertEqual(frappe.db.get_value("Sales Order", self.so_closed_with_money, "status"), "Closed")

	def test_erpnext_reopen_guard_leaves_other_closed_orders_alone(self):
		frappe.db.set_value("Sales Order", self.so_closed_with_money, malipopay.CLOSED_FOR_GOOD_FIELD, 0)
		doc = frappe.get_doc("Sales Order", self.so_closed_with_money)
		doc.status = "To Deliver and Bill"
		malipopay.before_change_sales_order(doc)  # no refusal

	def test_erpnext_reopen_guard_ignores_orders_that_are_not_plot_sales(self):
		# leases, trading and every other app's orders: returns before any database read
		doc = frappe._dict(docstatus=1, status="To Deliver and Bill", plot=None, name="SO-NOT-A-PLOT")
		with patch.object(frappe.db, "get_value") as read:
			malipopay.before_change_sales_order(doc)
		read.assert_not_called()

	def test_close_for_good_only_on_a_closed_order(self):
		bill = self._bill(self.so_open_with_money, state="Active")
		frappe.db.set_value(
			"Sales Order",
			self.so_open_with_money,
			"control_number",
			bill.control_number,
			update_modified=False,
		)
		with self.assertRaises(frappe.ValidationError):
			malipopay.close_for_good_at_malipopay(self.so_open_with_money)

	def test_close_for_good_is_system_manager_only(self):
		frappe.set_user("Guest")
		# frappe.only_for lets everyone through while tests run; switch that off
		with patch.dict(frappe.local.flags, {"in_test": False}), self.assertRaises(frappe.PermissionError):
			malipopay.close_for_good_at_malipopay(self.so_closed_with_money)

	def test_bill_form_will_not_expire_a_paused_bill(self):
		bill = self._bill(self.so_closed_with_money, state="Suspended")
		out = bill.expire_at_malipopay()
		self.assertFalse(out["ok"])
		self.assertIn("Close for good", out["message"])
		self.assertEqual(self._reload(bill).state, "Suspended")

	# ------------------------------------------------------------------ the hourly postman

	def test_hourly_pass_suspends_closed_with_money_and_expires_closed_without(self):
		paused = self._bill(self.so_closed_with_money, state="Active")
		stopped = self._bill(self.so_closed_no_money, state="Active")
		with (
			patch("landms.malipopay.expire_collection"),
			patch("landms.malipopay.suspend_collection"),
			patch("landms.malipopay.resume_collection"),
		):
			malipopay.kill_pending_collections(limit=500)
		self.assertEqual(self._reload(paused).state, "Suspend Requested")
		self.assertEqual(self._reload(stopped).state, "Kill Requested")

	def test_hourly_pass_leaves_a_suspended_bill_alone(self):
		bill = self._bill(self.so_closed_with_money, state="Suspended")
		with (
			patch("landms.malipopay.expire_collection"),
			patch("landms.malipopay.suspend_collection"),
			patch("landms.malipopay.resume_collection"),
		):
			malipopay.kill_pending_collections(limit=500)
		self.assertEqual(self._reload(bill).state, "Suspended")

	# ------------------------------------------------------------------ open orders follow the balance

	def _open_order(self, owes):
		rows = frappe.db.sql(
			f"""select so.name from `tabSales Order` so
			join `tabSales Invoice` si on si.name = so.plot_sales_invoice
			where so.docstatus = 1 and so.status != 'Closed' and si.docstatus = 1
			  and si.outstanding_amount {">" if owes else "<="} 0
			order by so.creation desc limit 50""",
			pluck=True,
		)
		for name in rows:
			if not malipopay._stopped_for_good(name):
				return name
		self.skipTest("this site has no open order of the kind the test needs")

	def _hourly_pass(self):
		with (
			patch("landms.malipopay.expire_collection"),
			patch("landms.malipopay.suspend_collection"),
			patch("landms.malipopay.resume_collection"),
		):
			malipopay.kill_pending_collections(limit=500)

	def test_hourly_pass_leaves_the_bill_of_a_fully_paid_open_order_open(self):
		# operator, 15 Oct 2026: nothing is sent; an open bill still hears about
		# a reversed payment, so the customer can pay again on the same number
		bill = self._bill(self._open_order(owes=False), state="Active")
		self._hourly_pass()
		self.assertEqual(self._reload(bill).state, "Active")
		self.assertFalse([log for log in self.logs if log.get("event_type") == "Suspend"])

	def test_hourly_pass_resumes_a_paused_bill_when_the_order_owes_again(self):
		bill = self._bill(self._open_order(owes=True), state="Suspended")
		self._hourly_pass()
		self.assertEqual(self._reload(bill).state, "Resume Requested")

	def test_hourly_pass_leaves_the_bill_of_an_open_order_that_still_owes(self):
		bill = self._bill(self._open_order(owes=True), state="Active")
		self._hourly_pass()
		self.assertEqual(self._reload(bill).state, "Active")

	# ------------------------------------------------------------------ orders from before Malipopay stay with TCB

	def _order_without_any_bill(self):
		for name in frappe.get_all(
			"Sales Order",
			filters={"docstatus": 1, "plot": ["is", "set"], "control_number": ["is", "set"]},
			pluck="name",
			order_by="creation asc",
			limit=200,
		):
			if not malipopay._sold_with_malipopay(frappe.get_doc("Sales Order", name)):
				return name
		self.skipTest("every order on this site has a Malipopay bill")

	def test_reopening_an_order_from_before_malipopay_raises_no_bill(self):
		# operator, 15 Oct 2026: old orders keep doing what they were doing
		name = self._order_without_any_bill()
		with (
			patch("landms.malipopay.collects_via_malipopay", return_value=True),
			patch("landms.malipopay._amount_still_owed", return_value=1000),
		):
			malipopay.reopen_collection_for_sales_order(name)
		self.assertFalse(frappe.db.exists("Malipopay Collection", {"sales_order": name}))

	def test_reopening_a_malipopay_order_whose_bill_ended_raises_a_new_one(self):
		name = self._order_without_any_bill()
		number = frappe.db.get_value("Sales Order", name, "control_number")
		dead = self._bill(name, state="Dead")
		frappe.db.set_value("Malipopay Collection", dead.name, "control_number", number)
		with (
			patch("landms.malipopay.collects_via_malipopay", return_value=True),
			patch("landms.malipopay._amount_still_owed", return_value=1000),
		):
			malipopay.reopen_collection_for_sales_order(name)
		self.assertTrue(
			frappe.db.exists("Malipopay Collection", {"sales_order": name, "state": ["!=", "Dead"]})
		)

	def test_an_amendment_of_an_order_from_before_malipopay_raises_no_bill(self):
		original = self._order_without_any_bill()
		amended = frappe.get_doc("Sales Order", original).as_dict()
		amended.update(name="TEST-AMEND-OLD", amended_from=original, control_number="TESTAMENDOLD01")
		with patch("landms.malipopay.collects_via_malipopay", return_value=True):
			malipopay._stage_collection_for_sales_order(frappe._dict(amended))
		self.assertFalse(frappe.db.exists("Malipopay Collection", {"control_number": "TESTAMENDOLD01"}))

	def test_the_order_form_says_nothing_on_an_order_from_before_malipopay(self):
		name = self._order_without_any_bill()
		with patch("landms.malipopay.collects_via_malipopay", return_value=True):
			self.assertEqual(malipopay.sales_order_malipopay_notice(name), {})
