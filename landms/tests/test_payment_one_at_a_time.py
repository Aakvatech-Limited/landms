"""Payments on one order are recorded one at a time, whichever rail they come from.

Two payments landing together used to read the same balance, and the invoice
then lost one of them (proven on a local site: 4 runs out of 4). Now each
payment takes the order's lock, and one that read figures another payment has
since changed is refused, unsaved. The rails (TCB notice and reconciliation,
Malipopay booking) then wait and record it again with fresh figures — TCB never
resends a notice (proven 14 Oct 2026: the second of two simultaneous notices was
refused before this; with it both are recorded and the invoice matches the
ledger, ~/Desktop/plot-audit/race_proof_tcb_notice.py).
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import payment_sync


class TestPaymentOneAtATime(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		row = frappe.db.sql(
			"""select so.name, so.plot_sales_invoice, si.outstanding_amount
			from `tabSales Order` so join `tabSales Invoice` si on si.name = so.plot_sales_invoice
			where so.docstatus = 1 and si.docstatus = 1 limit 1""",
			as_dict=True,
		)
		if not row:
			self.skipTest("this site has no Sales Order with a plot invoice")
		self.so, self.si, self.outstanding = row[0].name, row[0].plot_sales_invoice, row[0].outstanding_amount

	def test_unchanged_balance_is_accepted(self):
		payment_sync._refuse_if_read_before_another_payment(
			"Sales Invoice", self.si, "outstanding_amount", self.outstanding
		)

	def test_balance_changed_by_another_payment_is_refused(self):
		with self.assertRaises(payment_sync.ConcurrentPaymentError):
			payment_sync._refuse_if_read_before_another_payment(
				"Sales Invoice", self.si, "outstanding_amount", self.outstanding + 30
			)

	def test_invoice_created_by_another_first_payment_is_refused(self):
		# a second "first payment" must not build a second plot invoice
		with self.assertRaises(payment_sync.ConcurrentPaymentError):
			payment_sync._refuse_if_read_before_another_payment(
				"Sales Order", self.so, "plot_sales_invoice", None
			)

	def test_lock_is_held_until_the_transaction_ends(self):
		lock = f"plot_pay:{self.so}"[:64]
		payment_sync._lock_sales_order_for_payment(self.so)
		self.assertEqual(frappe.db.sql("SELECT IS_FREE_LOCK(%s)", (lock,))[0][0], 0)
		frappe.db.rollback()  # a commit releases it the same way
		self.assertEqual(frappe.db.sql("SELECT IS_FREE_LOCK(%s)", (lock,))[0][0], 1)


class TestRailsWaitForOtherPayments(FrappeTestCase):
	"""record_payment_waiting_for_others: the rails' way in."""

	def setUp(self):
		self.commits = 0

		def count_commit(*args, **kwargs):
			self.commits += 1

		p = patch.object(frappe.db, "commit", count_commit)
		p.start()
		self.addCleanup(p.stop)

	def test_a_clash_waits_and_records_with_fresh_figures(self):
		answers = [payment_sync.ConcurrentPaymentError("clash"), "PE-TEST-1"]

		def recorder(**kwargs):
			answer = answers.pop(0)
			if isinstance(answer, Exception):
				raise answer
			return answer

		with patch("landms.payment_sync.create_payment_entry_for_sales_order", side_effect=recorder):
			self.assertEqual(
				payment_sync.record_payment_waiting_for_others(sales_order_name="SO-T"), "PE-TEST-1"
			)
		self.assertEqual(self.commits, 1)  # one fresh start between the attempts

	def test_it_gives_up_after_repeated_clashes(self):
		with (
			patch(
				"landms.payment_sync.create_payment_entry_for_sales_order",
				side_effect=payment_sync.ConcurrentPaymentError("clash"),
			) as recorder,
			self.assertRaises(payment_sync.ConcurrentPaymentError),
		):
			payment_sync.record_payment_waiting_for_others(sales_order_name="SO-T")
		self.assertEqual(recorder.call_count, payment_sync.CONCURRENT_ATTEMPTS)

	def test_other_errors_are_not_retried(self):
		with (
			patch(
				"landms.payment_sync.create_payment_entry_for_sales_order",
				side_effect=frappe.ValidationError("no outstanding balance"),
			) as recorder,
			self.assertRaises(frappe.ValidationError),
		):
			payment_sync.record_payment_waiting_for_others(sales_order_name="SO-T")
		self.assertEqual(recorder.call_count, 1)
		self.assertEqual(self.commits, 0)

	def test_tcb_and_malipopay_use_the_waiting_recorder(self):
		import inspect

		from landms import malipopay, tcb

		self.assertIn(
			"record_payment_waiting_for_others", inspect.getsource(tcb.apply_tcb_payment_to_sales_order)
		)
		self.assertIn("record_payment_waiting_for_others", inspect.getsource(malipopay._book_one_receipt))
