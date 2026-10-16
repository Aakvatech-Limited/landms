"""LandMS Plot Inventory: the money cards agree with the stock ledger.

Operator, 14 Oct 2026: the report must show what is in stock, in sync with the
stock ledger. A plot leaves stock only at handover, so the cards count every
plot that is not Delivered / Title Closed. Read-only; nothing is written.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from landms.landms.report.landms_plot_inventory import landms_plot_inventory as report


def _row(status, cost, price):
	return {"status": status, "allocated_cost": cost, "selling_price": price, "margin": price - cost}


class TestPlotInventoryReport(FrappeTestCase):
	def _cards(self, data):
		return {card["label"]: card["value"] for card in report.get_summary(data)}

	def test_money_cards_count_every_plot_not_yet_handed_over(self):
		data = [
			_row("Available", 100, 1000),
			_row("Pending Advance", 10, 100),
			_row("Reserved", 20, 200),
			_row("Ready for Handover", 30, 300),
			_row("Delivered", 500, 5000),
			_row("Title Closed", 700, 7000),
		]
		cards = self._cards(data)
		self.assertEqual(cards["Inventory Cost, in stock (TZS)"], 160)
		self.assertEqual(cards["Asking Value, in stock (TZS)"], 1600)
		self.assertEqual(cards["Potential Margin, in stock (TZS)"], 1440)
		self.assertEqual(cards["Total Plots"], 6)
		self.assertEqual(cards["Delivered"], 2)

	def test_a_report_of_delivered_plots_only_has_nothing_in_stock(self):
		cards = self._cards([_row("Delivered", 500, 5000)])
		self.assertEqual(cards["Inventory Cost, in stock (TZS)"], 0)
		self.assertEqual(cards["Margin %, in stock"], 0)

	def test_inventory_cost_equals_the_stock_ledger_on_this_site(self):
		warehouse = frappe.db.get_single_value("LandMS Settings", "plot_inventory_warehouse")
		if not warehouse:
			self.skipTest("no plot warehouse set on this site")
		_columns, data, _message, _chart, summary = report.execute({})
		cards = {card["label"]: card["value"] for card in summary}
		ledger = frappe.db.sql(
			"""select coalesce(sum(stock_value_difference), 0) from `tabStock Ledger Entry`
			where warehouse = %s and is_cancelled = 0""",
			warehouse,
		)[0][0]
		self.assertAlmostEqual(flt(cards.get("Inventory Cost, in stock (TZS)", 0)), flt(ledger), delta=1)

	def test_the_filters_load_under_the_report_name(self):
		import os

		folder = os.path.dirname(report.__file__)
		with open(os.path.join(folder, "landms_plot_inventory.js")) as f:
			script = f.read()
		name = frappe.get_file_json(os.path.join(folder, "landms_plot_inventory.json"))["name"]
		self.assertIn(f'frappe.query_reports["{name}"]', script)
