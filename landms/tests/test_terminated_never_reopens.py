"""A terminated contract is final: its order is never reopened.

Operator rule, 14 Oct 2026. The order stays Closed (its invoice, payments and
forfeiture are the record) and neither LandMS's Reopen nor ERPNext's
Status > Re-open may lift it. Other closed plot orders keep ERPNext's un-close
(the console cancel recipe for unpaid closed orders relies on it). Every write
rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import sales_order_hooks


class TestTerminatedNeverReopens(FrappeTestCase):
	def setUp(self):
		p = patch.object(frappe.db, "commit", lambda *a, **k: None)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)
		row = frappe.db.sql("""select pc.name, pc.sales_order from `tabPlot Contract` pc
			join `tabSales Order` so on so.name = pc.sales_order
			where pc.docstatus = 1 and so.docstatus = 1 and ifnull(so.plot, '') != '' limit 1""")
		if not row:
			self.skipTest("this site has no submitted contract on a submitted plot order")
		self.contract, self.so = row[0]
		frappe.db.set_value("Sales Order", self.so, "status", "Closed", update_modified=False)

	def _terminate(self):
		frappe.db.set_value(
			"Plot Contract", self.contract, "contract_status", "Terminated", update_modified=False
		)

	def test_landms_reopen_refuses_a_terminated_order(self):
		self._terminate()
		with self.assertRaisesRegex(frappe.ValidationError, "termination is final"):
			sales_order_hooks.reopen_sales_order(self.so)

	def test_erpnext_reopen_refuses_a_terminated_order(self):
		self._terminate()
		with self.assertRaisesRegex(frappe.ValidationError, "termination is final"):
			frappe.get_doc("Sales Order", self.so).update_status("Draft")
		self.assertEqual(frappe.db.get_value("Sales Order", self.so, "status"), "Closed")

	def test_other_closed_plot_orders_can_still_be_unclosed(self):
		frappe.db.set_value(
			"Plot Contract", self.contract, "contract_status", "Ongoing", update_modified=False
		)
		doc = frappe.get_doc("Sales Order", self.so)
		doc.status = "To Deliver and Bill"
		sales_order_hooks.before_change_sales_order(doc)  # no refusal

	def test_an_order_staying_closed_is_not_checked(self):
		self._terminate()
		doc = frappe.get_doc("Sales Order", self.so)  # status Closed in memory too
		sales_order_hooks.before_change_sales_order(doc)

	def test_non_plot_orders_are_ignored(self):
		doc = frappe._dict(docstatus=1, status="To Deliver and Bill", plot=None, name="SO-NOT-A-PLOT")
		with patch.object(frappe.db, "get_value") as read:
			sales_order_hooks.before_change_sales_order(doc)
		read.assert_not_called()
