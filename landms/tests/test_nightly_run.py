"""The nightly run gets through even when something else is busy at midnight.

Operator, 14 Oct 2026: the nightly cancellations must not be blocked by the
monthly depreciation posting or a slow TCB, and must never leave an order
half-done. Live evidence: 1 Sep lock-wait failures; 13 Oct PA-2026-3053 — the
application was cancelled first and TCB did not answer the order's decline.
Every write rolls back; nothing is sent to a bank.
"""

import time
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import tasks


class TestNightlyRun(FrappeTestCase):
	def setUp(self):
		for target in ("frappe.db.commit", "frappe.db.rollback"):
			p = patch(target)
			p.start()
			self.addCleanup(p.stop)
		p = patch("landms.tasks.time.sleep")
		self.sleep = p.start()
		self.addCleanup(p.stop)
		p = patch("frappe.log_error")
		self.logged = p.start()
		self.addCleanup(p.stop)

	# ------------------------------------------------------------------ a busy database is waited out

	def test_a_busy_database_is_waited_out_and_tried_again(self):
		work = MagicMock(side_effect=[frappe.QueryTimeoutError("lock wait timeout"), None])
		self.assertTrue(tasks._process_one("expire Plot Application PA-TEST", work))
		self.assertEqual(work.call_count, 2)
		self.sleep.assert_called_once_with(tasks.LOCK_PAUSE_SECONDS)

	def test_a_deadlock_is_also_tried_again(self):
		work = MagicMock(side_effect=[frappe.QueryDeadlockError("deadlock"), None])
		self.assertTrue(tasks._process_one("expire Plot Application PA-TEST", work))

	def test_still_busy_after_the_last_try_is_logged_for_the_next_run(self):
		work = MagicMock(side_effect=frappe.QueryTimeoutError("lock wait timeout"))
		self.assertFalse(tasks._process_one("expire Plot Application PA-TEST", work))
		self.assertEqual(work.call_count, tasks.LOCK_ATTEMPTS)
		self.assertIn("stayed busy", self.logged.call_args.args[1])

	def test_other_failures_are_not_retried(self):
		work = MagicMock(side_effect=frappe.ValidationError("bank did not answer"))
		self.assertFalse(tasks._process_one("expire Plot Application PA-TEST", work))
		self.assertEqual(work.call_count, 1)
		self.sleep.assert_not_called()
		self.assertEqual(self.logged.call_args.args[1], "LandMS: Failed to expire Plot Application PA-TEST")

	# ------------------------------------------------------------------ never cut off half-way

	def test_a_run_near_its_time_limit_hands_the_rest_to_a_follow_up_job(self):
		def first_step(attempted, started):
			attempted.add("PA-DONE-TONIGHT")
			return "out of time"

		with (
			patch("landms.tasks.frappe.enqueue") as enqueue,
			patch("landms.tasks.auto_cancel_stale_unpaid_applications", side_effect=first_step),
			patch("landms.tasks.auto_expire_paid_applications_past_deadline") as expire,
		):
			tasks.daily(attempted=["PA-DONE-EARLIER"])
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
		self.assertIn("PA-DONE-EARLIER", enqueue.call_args.kwargs["attempted"])
		self.assertIn("PA-DONE-TONIGHT", enqueue.call_args.kwargs["attempted"])
		expire.assert_not_called()

	def test_each_step_stops_taking_orders_once_the_time_is_up(self):
		rows = [frappe._dict(name="PA-ONE", plot="P1", customer="C1")]
		long_ago = time.monotonic() - tasks.RUN_BUDGET_SECONDS - 1
		with (
			patch("landms.tasks._unpaid_applications_older_than", return_value=rows),
			patch("landms.tasks._process_one") as process,
		):
			attempted = set()
			result = tasks.auto_cancel_stale_unpaid_applications(attempted=attempted, started=long_ago)
		self.assertEqual(result, "out of time")
		process.assert_not_called()
		self.assertNotIn("PA-ONE", attempted)  # left for the follow-up job

	# ------------------------------------------------------------------ one pass at a time, errors kept

	def test_a_second_pass_does_not_run_alongside_one_still_working(self):
		with (
			patch("landms.tasks._take_nightly_lock", return_value=False),
			patch("landms.tasks._run_nightly_jobs") as run,
			patch("landms.tasks.frappe.enqueue") as enqueue,
		):
			tasks.daily()
		run.assert_not_called()
		enqueue.assert_not_called()
		self.assertEqual(self.logged.call_args.args[0], "LandMS: nightly run not started")

	def test_the_follow_up_job_is_queued_only_after_the_lock_is_released(self):
		calls = []
		with (
			patch("landms.tasks._take_nightly_lock", return_value=True),
			patch("landms.tasks._run_nightly_jobs", return_value=["PA-ONE"]),
			patch("landms.tasks._release_nightly_lock", side_effect=lambda: calls.append("release")),
			patch("landms.tasks.frappe.enqueue", side_effect=lambda *a, **k: calls.append("follow-up")),
		):
			tasks.daily()
		self.assertEqual(calls, ["release", "follow-up"])

	def test_the_lock_is_released_even_when_a_step_crashes(self):
		with (
			patch("landms.tasks._take_nightly_lock", return_value=True),
			patch("landms.tasks._run_nightly_jobs", side_effect=RuntimeError("boom")),
			patch("landms.tasks._release_nightly_lock") as release,
			self.assertRaises(RuntimeError),
		):
			tasks.daily()
		release.assert_called_once()

	def test_the_real_lock_can_be_taken_and_given_back(self):
		self.assertTrue(tasks._take_nightly_lock())
		tasks._release_nightly_lock()
		self.assertTrue(tasks._take_nightly_lock())
		tasks._release_nightly_lock()

	def test_a_failed_order_is_logged_and_the_log_is_kept(self):
		order = MagicMock()
		with (
			patch("landms.tasks.frappe.db.rollback", side_effect=lambda: order("rollback")),
			patch("landms.tasks.frappe.log_error", side_effect=lambda *a, **k: order("log")),
			patch("landms.tasks.frappe.db.commit", side_effect=lambda: order("commit")),
		):
			tasks._process_one("expire Plot Application PA-TEST", MagicMock(side_effect=ValueError("x")))
		self.assertEqual([c.args[0] for c in order.call_args_list], ["rollback", "log", "commit"])

	def test_an_order_still_busy_after_the_last_try_keeps_its_log_too(self):
		order = MagicMock()
		with (
			patch("landms.tasks.frappe.db.rollback", side_effect=lambda: order("rollback")),
			patch("landms.tasks.frappe.log_error", side_effect=lambda *a, **k: order("log")),
			patch("landms.tasks.frappe.db.commit", side_effect=lambda: order("commit")),
		):
			tasks._process_one(
				"expire Plot Application PA-TEST", MagicMock(side_effect=frappe.QueryTimeoutError("busy"))
			)
		self.assertEqual([c.args[0] for c in order.call_args_list][-3:], ["rollback", "log", "commit"])

	def test_a_follow_up_job_skips_what_tonight_already_handled(self):
		rows = [
			frappe._dict(name="PA-ONE", plot="P1", customer="C1"),
			frappe._dict(name="PA-TWO", plot="P2", customer="C2"),
		]
		with (
			patch("landms.tasks._unpaid_applications_older_than", return_value=rows),
			patch("landms.tasks._process_one", return_value=True) as process,
		):
			tasks.auto_cancel_stale_unpaid_applications(attempted={"PA-ONE"}, started=None)
		self.assertEqual([c.args[0] for c in process.call_args_list], ["cancel Plot Application PA-TWO"])

	# ------------------------------------------------------------------ never half-done

	def _order_with_number(self):
		row = frappe.db.sql("""select name, control_number from `tabSales Order`
			where docstatus = 1 and ifnull(control_number, '') != '' and ifnull(plot, '') != '' limit 1""")
		if not row:
			self.skipTest("this site has no submitted plot order with a payment number")
		return frappe._dict(name="PA-TEST-EXPIRE", plot="P-TEST", sales_order=row[0][0]), row[0][1]

	def test_the_number_is_released_before_anything_is_cancelled(self):
		app, number = self._order_with_number()
		calls = []
		application = MagicMock()
		application.cancel.side_effect = lambda: calls.append("cancel application")
		with (
			patch(
				"landms.sales_order_hooks.decline_reference_for_sales_order",
				side_effect=lambda so, cn: calls.append(("decline", cn)) or {"ok": True},
			),
			patch("landms.tasks.frappe.get_doc", return_value=application),
		):
			tasks._expire_without_advance(app)
		self.assertEqual(calls, [("decline", number), "cancel application"])

	def test_if_tcb_does_not_release_the_number_nothing_is_cancelled(self):
		app, _number = self._order_with_number()
		application = MagicMock()
		with (
			patch(
				"landms.sales_order_hooks.decline_reference_for_sales_order",
				return_value={"ok": False, "message": "TCB decline call failed with exception"},
			),
			patch("landms.tasks.frappe.get_doc", return_value=application),
			self.assertRaisesRegex(frappe.ValidationError, "Nothing was changed"),
		):
			tasks._expire_without_advance(app)
		application.cancel.assert_not_called()

	def test_an_application_without_an_order_is_simply_cancelled(self):
		application = MagicMock()
		with (
			patch("landms.sales_order_hooks.decline_reference_for_sales_order") as decline,
			patch("landms.tasks.frappe.get_doc", return_value=application),
		):
			tasks._expire_without_advance(frappe._dict(name="PA-TEST", plot="P", sales_order=None))
		decline.assert_not_called()
		application.cancel.assert_called_once()

	# ------------------------------------------------------------------ a running contract is left alone

	def _expire_run(self, contract):
		rows = [frappe._dict(name="PA-STALE", plot="P1", sales_order="SO-TEST", expiry_date="2026-09-01")]
		settings = frappe._dict(application_fee_validity_days=7)
		with (
			patch("landms.tasks.frappe.get_single", return_value=settings),
			patch("landms.tasks._paid_applications_past_deadline", return_value=rows),
			patch("landms.tasks._active_contract", return_value=contract),
			patch("landms.tasks._sales_order_has_advance_payment", return_value=True),
			patch("landms.tasks._process_one", return_value=True) as process,
		):
			attempted = set()
			tasks.auto_expire_paid_applications_past_deadline(attempted=attempted, started=None)
		return process, attempted

	def test_an_application_whose_contract_is_running_is_not_closed(self):
		process, attempted = self._expire_run(contract="PC-TEST-0001")
		process.assert_not_called()
		self.assertIn("PA-STALE", attempted)  # a follow-up job does not try it again

	def test_without_a_running_contract_the_expiry_goes_ahead(self):
		process, _attempted = self._expire_run(contract=None)
		self.assertEqual([c.args[0] for c in process.call_args_list], ["expire Plot Application PA-STALE"])

	def test_only_a_submitted_contract_counts_as_running(self):
		row = frappe.db.sql("""select sales_order from `tabPlot Contract`
			where docstatus = 1 and ifnull(sales_order, '') != '' limit 1""")
		if row:
			self.assertTrue(tasks._active_contract(row[0][0]))
		draft = frappe.db.sql("""select pc.sales_order from `tabPlot Contract` pc
			where pc.docstatus = 0 and ifnull(pc.sales_order, '') != ''
			  and not exists (select 1 from `tabPlot Contract` s
			                  where s.sales_order = pc.sales_order and s.docstatus = 1)
			limit 1""")
		if draft:
			self.assertIsNone(tasks._active_contract(draft[0][0]))
		if not row and not draft:
			self.skipTest("this site has no plot contracts")
		self.assertIsNone(tasks._active_contract(None))

	# ------------------------------------------------------------------ when and where it runs

	def test_the_nightly_run_starts_at_one_not_midnight(self):
		from landms import hooks

		events = hooks.scheduler_events
		self.assertEqual(events["cron"]["0 1 * * *"], ["landms.tasks.start_nightly_run"])
		for frequency in ("daily", "daily_long"):
			self.assertNotIn("landms.tasks.daily", events.get(frequency, []))

	def test_the_reconciliation_still_runs_first_at_midnight(self):
		from landms import hooks

		self.assertIn("landms.tasks.run_daily_tcb_reconciliation", hooks.scheduler_events["daily_long"])

	def test_the_starter_hands_the_run_to_the_long_queue_once(self):
		with patch("landms.tasks.frappe.enqueue") as enqueue:
			tasks.start_nightly_run()
		enqueue.assert_called_once()
		self.assertEqual(enqueue.call_args.args[0], "landms.tasks.daily")
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])


class TestNightlyRunChecksMalipopayFirst(FrappeTestCase):
	"""A customer who paid through Malipopay is never stopped by the 01:00 run
	because the payment notice was lost (Malipopay does not resend; the 04:40
	sweep comes after the run). Every write is rolled back."""

	def setUp(self):
		for target in ("frappe.db.commit", "frappe.db.rollback"):
			p = patch(target)
			p.start()
			self.addCleanup(p.stop)
		p = patch("frappe.log_error")
		self.logged = p.start()
		self.addCleanup(p.stop)

	def _check(self, *, on_malipopay=True, mode="Apply Payment", enabled=1, bills=("MPC-1",), result=None):
		booked = result or frappe._dict(ok=True, booked=[], suspense=0, failed=0)
		with (
			patch("landms.malipopay.collects_via_malipopay", return_value=on_malipopay),
			patch(
				"landms.malipopay.get_malipopay_settings",
				return_value=frappe._dict(enabled=enabled, inbound_mode=mode),
			),
			patch("landms.tasks._live_malipopay_bills", return_value=list(bills)),
			patch("landms.malipopay.book_receipts_for_collection", return_value=booked) as book,
		):
			return tasks._malipopay_money_waiting("SO-TEST"), book

	def test_an_order_on_a_tcb_only_project_is_not_held(self):
		waiting, book = self._check(on_malipopay=False)
		self.assertFalse(waiting)
		book.assert_not_called()

	def test_an_order_without_a_live_malipopay_bill_goes_ahead(self):
		# deploy day: Malipopay not set up, no bills anywhere
		waiting, book = self._check(bills=(), mode="Off", enabled=0)
		self.assertFalse(waiting)
		book.assert_not_called()
		self.logged.assert_not_called()

	def test_a_live_bill_that_cannot_be_checked_holds_the_order_and_tells_a_person(self):
		for mode, enabled in (("Log Only", 1), ("Off", 1), ("Apply Payment", 0)):
			self.logged.reset_mock()
			waiting, book = self._check(mode=mode, enabled=enabled)
			self.assertTrue(waiting)
			book.assert_not_called()
			self.logged.assert_called_once()
			self.assertIn("needs a look", self.logged.call_args.args[1])

	def test_nothing_waiting_at_malipopay_lets_the_run_go_ahead(self):
		waiting, book = self._check()
		self.assertFalse(waiting)
		book.assert_called_once_with("MPC-1")

	def test_money_found_holds_the_order_for_another_night(self):
		waiting, _book = self._check(result=frappe._dict(ok=True, booked=["PE-1"], suspense=0, failed=0))
		self.assertTrue(waiting)
		self.logged.assert_not_called()  # booked money is the good case: nothing to look at

	def test_money_that_could_not_be_booked_also_holds_the_order(self):
		for result in (
			frappe._dict(ok=True, booked=[], suspense=1, failed=0),
			frappe._dict(ok=True, booked=[], suspense=0, failed=1),
		):
			self.logged.reset_mock()
			waiting, _book = self._check(result=result)
			self.assertTrue(waiting)
			self.logged.assert_called_once()

	def test_malipopay_unreachable_holds_the_order_and_tells_a_person(self):
		waiting, _book = self._check(result=frappe._dict(ok=False, booked=[], suspense=0, failed=0))
		self.assertTrue(waiting)
		self.logged.assert_called_once()

	def test_an_error_while_checking_holds_the_order_and_is_logged(self):
		with patch("landms.malipopay.collects_via_malipopay", side_effect=RuntimeError("boom")):
			self.assertTrue(tasks._malipopay_money_waiting("SO-TEST"))
		self.logged.assert_called_once()

	def test_the_expiry_skips_an_order_with_malipopay_money(self):
		rows = [frappe._dict(name="PA-PAID", plot="P1", sales_order="SO-TEST", expiry_date="2026-09-01")]
		with (
			patch(
				"landms.tasks.frappe.get_single", return_value=frappe._dict(application_fee_validity_days=7)
			),
			patch("landms.tasks._paid_applications_past_deadline", return_value=rows),
			patch("landms.tasks._active_contract", return_value=None),
			patch("landms.tasks._malipopay_money_waiting", return_value=True),
			patch("landms.tasks._process_one") as process,
		):
			tasks.auto_expire_paid_applications_past_deadline(attempted=set(), started=None)
		process.assert_not_called()

	def test_a_contract_with_malipopay_money_is_not_terminated(self):
		row = frappe._dict(
			name="CTR-TEST",
			sales_order="SO-TEST",
			plot="P1",
			customer="C1",
			contract_status="Overdue",
			apply_auto_cancellation=1,
			payment_deadline="2026-09-01",
		)
		contract = MagicMock()
		with (
			patch("landms.tasks.frappe.db.exists", return_value=False),
			patch("landms.tasks._malipopay_money_waiting", return_value=True),
			patch("landms.tasks.frappe.get_doc", return_value=contract),
		):
			tasks._process_overdue_contract(row)
		contract.terminate_contract.assert_not_called()
