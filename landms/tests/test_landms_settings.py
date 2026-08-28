from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

MODULE = "landms.landms.doctype.landms_settings.landms_settings"


def _new_settings(**kwargs):
	doc = frappe.new_doc("LandMS Settings")
	doc.update(kwargs)
	return doc


class TestLandMSSettingsValidation(IntegrationTestCase):
	def test_validate_cost_center_requires_a_value(self):
		doc = _new_settings(cost_center=None)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_cost_center()

	def test_validate_cost_center_rejects_cross_company_cost_center(self):
		doc = _new_settings(cost_center="Main - LA", company="Land Co")
		with patch(f"{MODULE}.frappe.db.get_value", return_value="Other Co"):
			with self.assertRaises(frappe.ValidationError):
				doc._validate_cost_center()

	def test_check_root_type_rejects_mismatched_account_type(self):
		doc = _new_settings(company="Land Co")
		with patch(f"{MODULE}.frappe.db.get_value", return_value="Liability"):
			with self.assertRaises(frappe.ValidationError):
				doc._check_root_type("revenue_account", "Revenue - LA", "Income")

	def test_check_account_company_rejects_cross_company_account(self):
		doc = _new_settings(company="Land Co")
		with patch(f"{MODULE}.frappe.db.get_value", return_value="Other Co"):
			with self.assertRaises(frappe.ValidationError):
				doc._check_account_company("revenue_account", "Revenue - OC")

	def test_validate_fee_settings_rejects_negative_application_fee(self):
		doc = _new_settings(
			application_fee_amount=-1,
			unpaid_application_expiry_days=7,
			application_fee_validity_days=30,
		)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_fee_settings()

	def test_validate_fee_settings_rejects_zero_expiry_days(self):
		doc = _new_settings(
			application_fee_amount=0,
			unpaid_application_expiry_days=0,
			application_fee_validity_days=30,
		)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_fee_settings()

	def test_validate_fee_settings_accepts_valid_values(self):
		doc = _new_settings(
			application_fee_amount=0,
			unpaid_application_expiry_days=7,
			application_fee_validity_days=30,
		)
		doc._validate_fee_settings()  # must not raise

	def test_validate_no_account_overlap_rejects_same_advance_and_revenue_account(self):
		doc = _new_settings(customer_advance_account="Advances - LA", revenue_account="Advances - LA")
		with self.assertRaises(frappe.ValidationError):
			doc._validate_no_account_overlap()

	def test_validate_no_account_overlap_accepts_distinct_accounts(self):
		doc = _new_settings(customer_advance_account="Advances - LA", revenue_account="Revenue - LA")
		doc._validate_no_account_overlap()  # must not raise
