"""Execution smoke tests for every LandMS script report.

These run each report's real `execute({})` against the (empty) test site —
zero Land Acquisition / Sales Order / Plot records — so every SQL query,
JOIN and column definition is exercised for real, without needing to build
fixtures for eleven separate reports. A broken column reference, a bad JOIN,
or a v16 query-builder incompatibility fails loudly here; the actual
row-level aggregation math is out of scope for a smoke test.
"""

import importlib

from frappe.tests import IntegrationTestCase

REPORT_MODULES = [
	"landms.landms.report.landms_business_trend.landms_business_trend",
	"landms.landms.report.landms_collections.landms_collections",
	"landms.landms.report.landms_executive_dashboard.landms_executive_dashboard",
	"landms.landms.report.landms_forfeitures.landms_forfeitures",
	"landms.landms.report.landms_government_payable.landms_government_payable",
	"landms.landms.report.landms_plot_allocations.landms_plot_allocations",
	"landms.landms.report.landms_plot_inventory.landms_plot_inventory",
	"landms.landms.report.landms_revenue.landms_revenue",
	"landms.landms.report.landms_revenue_recognition.landms_revenue_recognition",
	"landms.landms.report.landms_sales_pipeline.landms_sales_pipeline",
	"landms.landms.report.landms_unearned_revenue.landms_unearned_revenue",
]


class TestReportsExecuteCleanlyWithNoFilters(IntegrationTestCase):
	def test_every_report_executes_and_returns_columns_and_data(self):
		for module_path in REPORT_MODULES:
			with self.subTest(report=module_path):
				module = importlib.import_module(module_path)
				result = module.execute({})

				self.assertIsInstance(result, tuple)
				columns, data = result[0], result[1]
				self.assertIsInstance(columns, list)
				self.assertGreater(len(columns), 0, "report must define at least one column")
				self.assertIsInstance(data, list)

				for column in columns:
					fieldname = column.get("fieldname") if isinstance(column, dict) else None
					self.assertTrue(fieldname, f"{module_path}: column missing a fieldname — {column}")
