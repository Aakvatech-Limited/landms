from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from landms.landms.doctype.land_acquisition.land_acquisition import (
	_build_period_summary,
	_compute_totals,
	_empty_summary,
)


def _new_land_acquisition(**kwargs):
	doc = frappe.new_doc("Land Acquisition")
	doc.update(kwargs)
	return doc


class TestComputeTotals(IntegrationTestCase):
	def test_splits_by_supplier_type_and_sums_grand_totals(self):
		sellers = [{"committed": 100, "billed": 80, "paid": 60, "outstanding": 20, "unbilled_po": 20}]
		others = [{"committed": 50, "billed": 40, "paid": 40, "outstanding": 0, "unbilled_po": 10}]

		totals = _compute_totals(sellers, others, total_area_sqm=100)

		self.assertEqual(totals["seller_billed"], 80)
		self.assertEqual(totals["other_billed"], 40)
		self.assertEqual(totals["billed"], 120)
		self.assertEqual(totals["committed"], 150)
		self.assertEqual(totals["cost_per_sqm_tzs"], 1.2)

	def test_zero_area_does_not_divide_by_zero(self):
		totals = _compute_totals([], [], total_area_sqm=0)
		self.assertEqual(totals["cost_per_sqm_tzs"], 0.0)
		self.assertEqual(totals["billed"], 0.0)


class TestEmptySummary(IntegrationTestCase):
	def test_shape_has_all_total_keys_zeroed(self):
		summary = _empty_summary("LACQ-TEST-0001")
		self.assertEqual(summary["land_acquisition"], "LACQ-TEST-0001")
		self.assertEqual(summary["sellers"], [])
		self.assertEqual(summary["totals"]["billed"], 0.0)
		self.assertIn("je_billed", summary["totals"])


class TestBuildPeriodSummary(IntegrationTestCase):
	def test_formats_years_months_days(self):
		summary = _build_period_summary(1, 2, 3, 1 * 365 + 2 * 30 + 3)
		self.assertIn("1", summary)
		self.assertIn(str(1 * 365 + 2 * 30 + 3), summary)


class TestLandAcquisitionValidators(IntegrationTestCase):
	def test_validate_area_rejects_zero(self):
		doc = _new_land_acquisition(total_area_sqm=0)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_area()

	def test_validate_area_accepts_positive(self):
		doc = _new_land_acquisition(total_area_sqm=500)
		doc._validate_area()  # must not raise

	def test_validate_coordinates_requires_both_or_neither(self):
		doc = _new_land_acquisition(latitude=-6.8, longitude=None)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_coordinates()

	def test_validate_coordinates_rejects_out_of_range_latitude(self):
		doc = _new_land_acquisition(latitude=95, longitude=39.0)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_coordinates()

	def test_validate_coordinates_accepts_valid_pair(self):
		doc = _new_land_acquisition(latitude=-6.8, longitude=39.28)
		doc._validate_coordinates()  # must not raise

	def test_convert_payment_period_to_days_rejects_zero_period(self):
		doc = _new_land_acquisition(payment_years=0, payment_months=0, payment_days_input=0)
		with self.assertRaises(frappe.ValidationError):
			doc._convert_payment_period_to_days()

	def test_convert_payment_period_to_days_sums_components(self):
		doc = _new_land_acquisition(payment_years=1, payment_months=2, payment_days_input=5)
		doc._convert_payment_period_to_days()
		self.assertEqual(doc.payment_completion_days, 365 + 60 + 5)

	def test_validate_sales_defaults_rejects_out_of_range_percent(self):
		doc = _new_land_acquisition(
			booking_fee_percent=150, government_share_percent=10, payment_completion_days=30
		)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_sales_defaults()

	def test_validate_and_set_tcb_patterns_requires_hyphenated_partner_code(self):
		doc = _new_land_acquisition(partner_code="NOHYPHEN")
		with self.assertRaises(frappe.ValidationError):
			doc._validate_and_set_tcb_patterns()

	def test_validate_and_set_tcb_patterns_derives_pattern_from_suffix(self):
		doc = _new_land_acquisition(partner_code="PART-GVA")
		doc._validate_and_set_tcb_patterns()
		self.assertEqual(doc.control_number_pattern, "99910#GVA####")
		self.assertEqual(doc.related_control_number_pattern, "9992#RGVA####")

	def test_validate_plot_type_rates_rejects_duplicate_plot_type(self):
		doc = _new_land_acquisition()
		doc.set("plot_type_rates", [])
		doc.append("plot_type_rates", {"plot_type": "Residential", "rate": 100})
		doc.append("plot_type_rates", {"plot_type": "Residential", "rate": 120})
		with self.assertRaises(frappe.ValidationError):
			doc._validate_plot_type_rates()

	def test_before_submit_requires_approved_status(self):
		doc = _new_land_acquisition(status="Draft")
		with self.assertRaises(frappe.ValidationError):
			doc.before_submit()

	def test_block_if_active_plots_allows_cancel_when_no_submitted_plots(self):
		doc = _new_land_acquisition()
		doc.name = "LACQ-TEST-0001"
		with (
			patch(
				"landms.landms.doctype.land_acquisition.land_acquisition.frappe.db.exists", return_value=True
			),
			patch("landms.landms.doctype.land_acquisition.land_acquisition.frappe.db.count", return_value=0),
		):
			doc._block_if_active_plots()  # must not raise

	def test_block_if_active_plots_blocks_cancel_when_plots_submitted(self):
		doc = _new_land_acquisition()
		doc.name = "LACQ-TEST-0001"
		sample = [frappe._dict(name="PLOT-0001")]
		with (
			patch(
				"landms.landms.doctype.land_acquisition.land_acquisition.frappe.db.exists", return_value=True
			),
			patch("landms.landms.doctype.land_acquisition.land_acquisition.frappe.db.count", return_value=1),
			patch(
				"landms.landms.doctype.land_acquisition.land_acquisition.frappe.db.get_all",
				return_value=sample,
			),
		):
			with self.assertRaises(frappe.ValidationError):
				doc._block_if_active_plots()
