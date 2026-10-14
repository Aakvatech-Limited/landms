"""A TCB payment that fails half-way leaves nothing half-done.

Live, 7 Sep 2026: payments were saved with no government share and no order
or contract update, because a step after the Payment Entry failed and the next
log write saved what was there. Now the failed payment undoes its own writes,
and only those: the notice record made before it stays. Nothing is sent to a
bank; every write is rolled back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from landms import tcb


class TestTcbPaymentUndo(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.local.conf.developer_mode = 1  # this code's own hooks
		self.addCleanup(lambda: frappe.local.conf.pop("developer_mode", None))
		row = frappe.db.sql(
			"""select so.name, so.control_number, si.outstanding_amount
			from `tabSales Order` so join `tabSales Invoice` si on si.name = so.plot_sales_invoice
			where so.docstatus = 1 and so.status not in ('Closed', 'Cancelled')
			  and ifnull(so.control_number, '') != '' and si.docstatus = 1 and si.outstanding_amount > 100
			limit 1""",
			as_dict=True,
		)
		if not row:
			self.skipTest("this site has no open plot order with an outstanding invoice")
		self.order = row[0]

	def _pay(self, reference):
		return tcb.apply_tcb_payment_to_sales_order(
			control_number=self.order.control_number,
			amount=100,
			payment_date=None,
			payment_reference=reference,
			hold_for_review=False,
		)

	def test_a_payment_failing_half_way_leaves_no_payment_entry_and_keeps_the_notice(self):
		# stands in for the notice record the handler saves before the payment
		marker = frappe.get_doc({"doctype": "ToDo", "description": "notice saved before the payment"}).insert(
			ignore_permissions=True
		)
		with patch(
			"landms.payment_sync._post_government_share_je",
			side_effect=frappe.ValidationError("cost centre refused"),
		) as govt_share:
			result = self._pay("TEST-UNDO-0001")
		govt_share.assert_called()  # the payment really got as far as the government share
		self.assertFalse(result["ok"])
		self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": "TEST-UNDO-0001"}))
		self.assertTrue(frappe.db.exists("ToDo", marker.name))

	def test_a_payment_that_succeeds_is_booked(self):
		before = flt(
			frappe.db.get_value(
				"Sales Invoice",
				frappe.db.get_value("Sales Order", self.order.name, "plot_sales_invoice"),
				"outstanding_amount",
			)
		)
		with patch("landms.payment_sync._enqueue_plot_payment_sync"):
			result = self._pay("TEST-UNDO-0002")
		self.assertTrue(result["ok"], result.get("message"))
		self.assertTrue(frappe.db.exists("Payment Entry", {"reference_no": "TEST-UNDO-0002", "docstatus": 1}))
		after = flt(
			frappe.db.get_value(
				"Sales Invoice",
				frappe.db.get_value("Sales Order", self.order.name, "plot_sales_invoice"),
				"outstanding_amount",
			)
		)
		self.assertAlmostEqual(before - after, 100, delta=0.01)

	def test_a_registry_hiccup_does_not_undo_a_booked_payment(self):
		with (
			patch("landms.payment_sync._enqueue_plot_payment_sync"),
			patch(
				"landms.landms.doctype.tcb_control_number.tcb_control_number.TCBControlNumber.mark_paid",
				side_effect=frappe.ValidationError("transition"),
			),
			patch("frappe.log_error") as logged,
		):
			result = self._pay("TEST-UNDO-0003")
		self.assertTrue(result["ok"])
		self.assertTrue(frappe.db.exists("Payment Entry", {"reference_no": "TEST-UNDO-0003", "docstatus": 1}))
		logged.assert_called_once()

	def test_a_payment_the_database_undid_is_not_reported_as_booked(self):
		def deadlock(*args, **kwargs):
			frappe.db.rollback()  # what MariaDB does to the whole transaction on a deadlock
			raise frappe.QueryDeadlockError("deadlock")

		with (
			patch("landms.payment_sync._enqueue_plot_payment_sync"),
			patch(
				"landms.landms.doctype.tcb_control_number.tcb_control_number.TCBControlNumber.mark_paid",
				side_effect=deadlock,
			) as registry,
			patch("frappe.log_error"),
		):
			result = self._pay("TEST-UNDO-0004")
		registry.assert_called_once()  # the payment really got as far as the registry
		self.assertFalse(result["ok"])
		self.assertIn("undone by the database", result["message"])
		self.assertFalse(frappe.db.exists("Payment Entry", {"reference_no": "TEST-UNDO-0004"}))
