from types import SimpleNamespace
from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from landms.landms.doctype.plot_application.plot_application import PlotApplication


class TestPlotApplicationAmendReset(FrappeTestCase):
	def test_amended_application_resets_payment_state(self):
		doc = PlotApplication(
			{
				"doctype": "Plot Application",
				"name": "PA-2026-0015-1",
				"amended_from": "PA-2026-0015",
				"status": "Cancelled",
				"customer": "Ekta",
				"plot": "PLT-2026-0012",
				"application_date": "2026-04-14",
				"payment_date": "2026-04-14",
				"reference_no": "APP-FEE-PLT-2026-0012",
				"expiry_date": "2026-04-21",
				"sales_invoice": "ACC-SINV-2026-00018",
				"payment_entry": "ACC-PAY-2026-00032",
				"sales_order": "SAL-ORD-2026-00010",
			}
		)

		with patch(
			"landms.landms.doctype.plot_application.plot_application.today",
			return_value="2026-04-15",
		):
			doc.reset_amended_application()

		self.assertEqual(doc.status, "Draft")
		self.assertEqual(str(doc.application_date), "2026-04-15")
		self.assertIsNone(doc.payment_date)
		self.assertEqual(doc.reference_no, "")
		self.assertIsNone(doc.expiry_date)
		self.assertEqual(doc.sales_invoice, "")
		self.assertEqual(doc.payment_entry, "")
		self.assertEqual(doc.sales_order, "")

	def test_non_amended_application_keeps_existing_values(self):
		doc = PlotApplication(
			{
				"doctype": "Plot Application",
				"name": "PA-2026-0016",
				"status": "Submitted",
				"customer": "Ekta",
				"plot": "PLT-2026-0013",
				"application_date": "2026-04-15",
				"payment_date": "2026-04-15",
				"reference_no": "REF-001",
				"expiry_date": "2026-04-22",
				"sales_invoice": "ACC-SINV-2026-00019",
				"payment_entry": "ACC-PAY-2026-00033",
				"sales_order": "SAL-ORD-2026-00011",
			}
		)

		doc.reset_amended_application()

		self.assertEqual(doc.status, "Submitted")
		self.assertEqual(str(doc.application_date), "2026-04-15")
		self.assertEqual(str(doc.payment_date), "2026-04-15")
		self.assertEqual(doc.reference_no, "REF-001")
		self.assertEqual(str(doc.expiry_date), "2026-04-22")
		self.assertEqual(doc.sales_invoice, "ACC-SINV-2026-00019")
		self.assertEqual(doc.payment_entry, "ACC-PAY-2026-00033")
		self.assertEqual(doc.sales_order, "SAL-ORD-2026-00011")


class TestPlotApplicationCancellation(FrappeTestCase):
	def test_cancel_linked_draft_sales_order_clears_link_before_delete(self):
		doc = PlotApplication(
			{
				"doctype": "Plot Application",
				"name": "PA-TEST-0001",
				"status": "Paid",
				"sales_order": "SAL-ORD-TEST-0001",
			}
		)

		with (
			patch(
				"landms.landms.doctype.plot_application.plot_application.frappe.db.exists",
				return_value=True,
			),
			patch(
				"landms.landms.doctype.plot_application.plot_application.frappe.get_doc",
				return_value=SimpleNamespace(name="SAL-ORD-TEST-0001", docstatus=0),
			),
			patch("landms.landms.doctype.plot_application.plot_application.frappe.db.set_value") as set_value,
			patch("landms.landms.doctype.plot_application.plot_application.frappe.delete_doc") as delete_doc,
		):
			doc._cancel_linked_sales_order_if_safe()

		set_value.assert_called_once_with(
			"Plot Application", "PA-TEST-0001", "sales_order", "", update_modified=False
		)
		delete_doc.assert_called_once_with("Sales Order", "SAL-ORD-TEST-0001", ignore_permissions=True)
		self.assertEqual(doc.sales_order, "")
