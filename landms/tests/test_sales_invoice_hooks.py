from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from landms.sales_invoice_hooks import before_save_sales_invoice


class _FakeSalesInvoice:
	"""Plain attribute holder: doc.items must return the child table, not
	dict.items — frappe._dict (a dict subclass) can't stand in here."""

	def __init__(self, **kwargs):
		self.__dict__.update(kwargs)

	def get(self, key, default=None):
		return getattr(self, key, default)

	def append(self, fieldname, row):
		getattr(self, fieldname).append(frappe._dict(row))


class TestBeforeSaveSalesInvoice(IntegrationTestCase):
	def _make_doc(self, *, is_plot_sale_invoice=1, items=None, payment_schedule=None, name="SI-TEST-0001"):
		return _FakeSalesInvoice(
			name=name,
			is_plot_sale_invoice=is_plot_sale_invoice,
			sales_order_reference=None,
			items=[frappe._dict(item) for item in (items or [])],
			payment_schedule=[frappe._dict(row) for row in (payment_schedule or [])],
		)

	def test_skips_non_plot_sale_invoices(self):
		doc = self._make_doc(is_plot_sale_invoice=0, items=[{"enable_deferred_revenue": 1}])
		before_save_sales_invoice(doc)
		self.assertEqual(doc.items[0].enable_deferred_revenue, 1)

	def test_clears_deferred_revenue_on_plot_sale_items(self):
		doc = self._make_doc(
			items=[
				{
					"enable_deferred_revenue": 1,
					"deferred_revenue_account": "Deferred Revenue - LA",
					"service_start_date": "2026-01-01",
					"service_end_date": "2026-12-31",
				}
			],
			payment_schedule=[{"payment_term": "Advance"}, {"payment_term": "Balance"}],
		)
		before_save_sales_invoice(doc)

		item = doc.items[0]
		self.assertEqual(item.enable_deferred_revenue, 0)
		self.assertEqual(item.deferred_revenue_account, "")
		self.assertIsNone(item.service_start_date)
		self.assertIsNone(item.service_end_date)

	def test_restores_collapsed_payment_schedule_from_sales_order(self):
		doc = self._make_doc(payment_schedule=[{"payment_term": "Advance"}])
		so_schedule = [
			frappe._dict(
				payment_term="Advance",
				description="Advance",
				due_date="2026-01-01",
				invoice_portion=20,
				payment_amount=100.0,
			),
			frappe._dict(
				payment_term="Balance",
				description="Balance",
				due_date="2026-06-01",
				invoice_portion=80,
				payment_amount=400.0,
			),
		]
		with (
			patch("landms.sales_invoice_hooks.frappe.db.get_value", return_value="SAL-ORD-0001"),
			patch("landms.sales_invoice_hooks.frappe.get_all", return_value=so_schedule),
		):
			before_save_sales_invoice(doc)

		self.assertEqual(len(doc.payment_schedule), 2)
		self.assertEqual(doc.payment_schedule[1].payment_term, "Balance")
		self.assertEqual(doc.payment_schedule[1].payment_amount, 400.0)

	def test_leaves_schedule_alone_when_already_has_multiple_rows(self):
		doc = self._make_doc(payment_schedule=[{"payment_term": "Advance"}, {"payment_term": "Balance"}])
		with patch("landms.sales_invoice_hooks.frappe.get_all") as mock_get_all:
			before_save_sales_invoice(doc)
		mock_get_all.assert_not_called()
		self.assertEqual(len(doc.payment_schedule), 2)
