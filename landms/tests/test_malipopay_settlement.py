"""Malipopay's cut of every Malipopay payment is recorded.

Operator, 14 Oct 2026: Malipopay takes a percentage of each payment and says
so in its own bill breakdown: its ledger posts every payment with the cut.
Until the agreement says what to do with it, every Malipopay payment gets a
Malipopay Settlement record from those figures, with no accounting entry, and
it can never undo or stop a booking.
Every write is rolled back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import malipopay

# Malipopay's own ledger lines on the live test bill ML330030 (14 Oct 2026),
# GVA's side only: 100 paid = 99 + 1 cut, then 500 paid = 495 + 5 cut.
LIVE_LEDGER = [
	{
		"scope": "PLATFORM",
		"eventType": "COLLECTION",
		"postedAt": "2026-10-14T12:29:49.479Z",
		"totalCr": 100,
		"lines": [
			{"account": "Float: M-Pesa", "accountType": "Assets", "side": "DR", "amount": 100},
			{"account": "Merchant Payable", "accountType": "Liabilities", "side": "CR", "amount": 99},
			{"account": "Platform Commission Revenue", "accountType": "Income", "side": "CR", "amount": 1},
		],
	},
	{
		"scope": "MERCHANT",
		"eventType": "COLLECTION",
		"postedAt": "2026-10-14T12:29:49.643Z",
		"totalCr": 100,
		"lines": [
			{"account": "Malipopay Wallet", "accountType": "Assets", "side": "DR", "amount": 99},
			{"account": "Platform Fees Expense", "accountType": "Expenses", "side": "DR", "amount": 1},
			{"account": "Sales Revenue", "accountType": "Income", "side": "CR", "amount": 100},
		],
	},
	{
		"scope": "MERCHANT",
		"eventType": "COLLECTION",
		"postedAt": "2026-10-14T20:47:31.163Z",
		"totalCr": 500,
		"lines": [
			{"account": "Malipopay Wallet", "accountType": "Assets", "side": "DR", "amount": 495},
			{"account": "Platform Fees Expense", "accountType": "Expenses", "side": "DR", "amount": 5},
			{"account": "Sales Revenue", "accountType": "Income", "side": "CR", "amount": 500},
		],
	},
]
# the bill's running totals on that read still showed the first payment only
LIVE_BILL = {"feePercent": 1, "feeAmount": 1, "netAmount": 99, "transactionFee": 0}


class TestMalipopaySettlement(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		if not frappe.db.table_exists("Malipopay Settlement"):
			self.skipTest("Malipopay Settlement is not installed on this site")
		p = patch("frappe.db.commit")
		p.start()
		self.addCleanup(p.stop)
		self.ledger = self._ledger_row("TXN-SETTLE-0001")
		self.bill = frappe._dict(name=None, sales_order=None, customer=None, control_number="9991000000000")

	def _ledger_row(self, txn):
		row = frappe.get_doc(
			{
				"doctype": "Payment Receipt Ledger",
				"source_rail": "Malipopay",
				"txn_id": txn,
				"status": "Booked",
				"amount": 100000,
			}
		)
		row.flags.ignore_links = True
		row.flags.ignore_mandatory = True
		row.insert(ignore_permissions=True)
		return row.name

	def _record(self, payment=None, amount=100000, ledger=None, ledger_lines=None, paid_at=None):
		breakdown = {"payment": payment or {}, "ledger": ledger_lines or []}
		malipopay._record_settlement(
			ledger or self.ledger, None, amount, self.bill, {"paidAt": paid_at}, breakdown
		)
		return frappe.db.get_value(
			"Malipopay Settlement",
			{"receipt": ledger or self.ledger},
			[
				"gross_amount",
				"fee_percent",
				"fee_amount",
				"net_amount",
				"fee_source",
				"transaction_fee",
				"status",
			],
			as_dict=True,
		)

	def test_the_cut_is_the_one_malipopay_posted_for_this_payment(self):
		row = self._record(
			LIVE_BILL, amount=500, ledger_lines=LIVE_LEDGER, paid_at="2026-10-14T20:47:27.221Z"
		)
		self.assertEqual((row.gross_amount, row.fee_amount, row.net_amount), (500, 5, 495))
		self.assertEqual((row.fee_source, row.fee_percent, row.transaction_fee), ("Malipopay Ledger", 1, 0))
		self.assertEqual(row.status, "Recorded")

	def test_malipopays_lagging_bill_totals_are_not_used(self):
		# the totals said cut 1 / net 99 while this payment was 500
		row = self._record(
			LIVE_BILL, amount=500, ledger_lines=LIVE_LEDGER, paid_at="2026-10-14T20:47:27.221Z"
		)
		self.assertNotEqual(row.fee_amount, 1)

	def test_the_platform_side_of_malipopays_ledger_is_not_read(self):
		row = self._record(
			LIVE_BILL, amount=100, ledger_lines=LIVE_LEDGER[:1], paid_at="2026-10-14T12:29:45.379Z"
		)
		self.assertEqual(row.fee_source, "Fee Percent")

	def test_of_two_payments_of_the_same_amount_the_nearest_line_counts(self):
		second = dict(LIVE_LEDGER[2], postedAt="2026-10-20T08:00:02.000Z")
		second["lines"] = [
			{"accountType": "Assets", "side": "DR", "amount": 490},
			{"accountType": "Expenses", "side": "DR", "amount": 10},
			{"accountType": "Income", "side": "CR", "amount": 500},
		]
		row = self._record(
			LIVE_BILL, amount=500, ledger_lines=[LIVE_LEDGER[2], second], paid_at="2026-10-20T08:00:00.000Z"
		)
		self.assertEqual(row.fee_amount, 10)

	def test_a_ledger_line_that_does_not_add_up_is_not_trusted(self):
		broken = dict(LIVE_LEDGER[2])
		broken["lines"] = [{"accountType": "Expenses", "side": "DR", "amount": 5}]
		row = self._record(LIVE_BILL, amount=500, ledger_lines=[broken])
		self.assertEqual((row.fee_amount, row.fee_source), (5, "Fee Percent"))

	def test_without_the_ledger_line_yet_the_cut_follows_malipopays_percentage(self):
		row = self._record({"feePercent": 1.5}, amount=100000)
		self.assertEqual((row.fee_amount, row.net_amount, row.fee_source), (1500, 98500, "Fee Percent"))

	def test_without_any_figure_from_malipopay_the_record_waits(self):
		# live test ML332079 (15 Oct 2026): the first payment's read had no
		# percentage and no ledger yet; both came four seconds later
		row = self._record({"feePercent": None, "transactionFee": 0}, amount=100)
		self.assertEqual(row.status, "Waiting for Malipopay")
		self.assertEqual(row.fee_amount, 0)
		self.assertFalse(row.fee_source)

	def _complete(self, payment, ledger_lines, collection="MPC-SETTLE-TEST"):
		frappe.db.set_value(
			"Malipopay Settlement", {"receipt": self.ledger}, "malipopay_collection", collection
		)
		frappe.db.set_value("Payment Receipt Ledger", self.ledger, "paid_at", "2026-10-15 00:00:59")
		malipopay.complete_waiting_settlements(collection, {"payment": payment, "ledger": ledger_lines})
		return frappe.db.get_value(
			"Malipopay Settlement",
			{"receipt": self.ledger},
			["status", "fee_amount", "net_amount", "fee_source", "fee_percent"],
			as_dict=True,
		)

	def test_the_next_read_of_the_bill_fills_in_a_waiting_record(self):
		self._record({"feePercent": None}, amount=100)
		later = [dict(LIVE_LEDGER[1], postedAt="2026-10-14T21:01:03.232Z")]
		row = self._complete(LIVE_BILL, later)
		self.assertEqual(
			(row.status, row.fee_amount, row.net_amount, row.fee_source, row.fee_percent),
			("Recorded", 1, 99, "Malipopay Ledger", 1),
		)

	def test_a_read_still_without_figures_leaves_the_record_waiting(self):
		self._record({"feePercent": None}, amount=100)
		row = self._complete({"feePercent": None}, [])
		self.assertEqual(row.status, "Waiting for Malipopay")

	def test_a_cut_from_the_percentage_is_replaced_by_the_one_malipopay_posted(self):
		self._record({"feePercent": 2}, amount=100)
		row = self._complete(LIVE_BILL, [LIVE_LEDGER[1]])
		self.assertEqual((row.fee_amount, row.fee_source), (1, "Malipopay Ledger"))

	def test_a_settled_record_is_never_changed(self):
		self._record({"feePercent": None}, amount=100)
		frappe.db.set_value("Malipopay Settlement", {"receipt": self.ledger}, "status", "Settled")
		row = self._complete(LIVE_BILL, [LIVE_LEDGER[1]])
		self.assertEqual((row.status, row.fee_amount), ("Settled", 0))

	def test_one_record_per_payment(self):
		self._record({"feePercent": 1})
		self._record({"feePercent": 1})
		self.assertEqual(frappe.db.count("Malipopay Settlement", {"receipt": self.ledger}), 1)

	def test_no_accounting_entry_is_made(self):
		before = frappe.db.count("Journal Entry")
		self._record({"feePercent": 1})
		self.assertEqual(frappe.db.count("Journal Entry"), before)

	def test_a_failure_never_breaks_the_booking(self):
		with (
			patch("landms.malipopay.frappe.get_doc", side_effect=RuntimeError("boom")),
			patch("landms.malipopay.frappe.db.rollback"),
			patch("frappe.log_error") as logged,
		):
			malipopay._record_settlement(
				self.ledger, None, 100000, self.bill, {}, {"payment": {"feePercent": 1}}
			)
		logged.assert_called_once()
