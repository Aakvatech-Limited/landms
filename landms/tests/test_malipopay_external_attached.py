"""A bill counts the bank money attached to its order after it was made.

Final review, 14 Oct 2026: judging by when the Payment Entry was created would
miss a payment moved onto the order later (Payment Reconciliation), and the
provider would ask the customer for too much. What counts is when the
payment's ledger rows against this order were written.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import malipopay_external as ext

BILL_MADE = "2026-10-10 12:00:00"


def _ledger(voucher, amount, creation):
	return frappe._dict(voucher_no=voucher, amount=-amount, creation=creation)


class TestExternalEntriesCountAttachedMoney(FrappeTestCase):
	def _held(self, rows):
		pe = frappe._dict(
			docstatus=1, posting_date="2026-01-01", creation="2026-01-01 08:00:00", payment_rail=""
		)
		with (
			patch("landms.malipopay_external._invoices_for_sales_order", return_value=["SINV-1"]),
			patch("landms.malipopay_external._malipopay_payment_entries", return_value=set()),
			patch("landms.malipopay_external.frappe.get_all", return_value=rows),
			patch("landms.malipopay_external.frappe.db.get_value", return_value=pe),
		):
			return [h[0] for h in ext.held_outside_malipopay("SO-1", since=BILL_MADE)]

	def test_money_attached_before_the_bill_is_not_counted_again(self):
		self.assertEqual(self._held([_ledger("PE-OLD", 500, "2026-10-01 09:00:00")]), [])

	def test_an_old_payment_moved_onto_the_order_after_the_bill_is_counted(self):
		# the Payment Entry itself is from January; its rows against this order are new
		self.assertEqual(self._held([_ledger("PE-MOVED", 500, "2026-10-12 09:00:00")]), ["PE-MOVED"])

	def test_without_a_bill_date_everything_counts(self):
		pe = frappe._dict(
			docstatus=1, posting_date="2026-01-01", creation="2026-01-01 08:00:00", payment_rail=""
		)
		with (
			patch("landms.malipopay_external._invoices_for_sales_order", return_value=["SINV-1"]),
			patch("landms.malipopay_external._malipopay_payment_entries", return_value=set()),
			patch(
				"landms.malipopay_external.frappe.get_all",
				return_value=[_ledger("PE-OLD", 500, "2026-10-01 09:00:00")],
			),
			patch("landms.malipopay_external.frappe.db.get_value", return_value=pe),
		):
			self.assertEqual([h[0] for h in ext.held_outside_malipopay("SO-1")], ["PE-OLD"])
