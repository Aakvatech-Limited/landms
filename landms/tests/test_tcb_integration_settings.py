from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from landms.landms.doctype.tcb_integration_settings.tcb_integration_settings import _looks_like_https_url

MODULE = "landms.landms.doctype.tcb_integration_settings.tcb_integration_settings"


def _new_settings(**kwargs):
	doc = frappe.new_doc("TCB Integration Settings")
	doc.update(kwargs)
	return doc


class TestLooksLikeHttpsUrl(IntegrationTestCase):
	def test_accepts_https_url(self):
		self.assertTrue(_looks_like_https_url("https://tcb.example.com/api"))

	def test_rejects_missing_scheme(self):
		self.assertFalse(_looks_like_https_url("tcb.example.com/api"))

	def test_rejects_garbage(self):
		self.assertFalse(_looks_like_https_url("not a url"))


class TestValidateIpnAuth(IntegrationTestCase):
	def test_off_mode_skips_token_check(self):
		doc = _new_settings(ipn_auth_mode="Off", ipn_auth_token=None)
		doc._validate_ipn_auth()  # must not raise

	def test_enforce_without_token_throws(self):
		doc = _new_settings(ipn_auth_mode="Enforce")
		with patch.object(doc, "get_password", return_value=""):
			with self.assertRaises(frappe.ValidationError):
				doc._validate_ipn_auth()

	def test_enforce_with_token_passes(self):
		doc = _new_settings(ipn_auth_mode="Enforce")
		with patch.object(doc, "get_password", return_value="secret-token"):
			doc._validate_ipn_auth()  # must not raise


class TestValidateLiveCredentials(IntegrationTestCase):
	def test_rejects_malformed_reference_url(self):
		doc = _new_settings(reference_create_url="not a url", enabled=0)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_live_credentials()

	def test_disabled_integration_skips_outbound_checks(self):
		doc = _new_settings(enabled=0, outbound_mode="Live")
		doc._validate_live_credentials()  # must not raise — enabled=0 short-circuits

	def test_live_outbound_without_urls_throws(self):
		doc = _new_settings(
			enabled=1,
			outbound_mode="Live",
			inbound_mode="Off",
			reference_create_url="",
			reference_decline_url="",
		)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_live_credentials()

	def test_live_outbound_with_urls_passes(self):
		doc = _new_settings(
			enabled=1,
			outbound_mode="Live",
			inbound_mode="Off",
			reference_create_url="https://tcb.example.com/create",
			reference_decline_url="https://tcb.example.com/decline",
		)
		doc._validate_live_credentials()  # must not raise


class TestValidateInboundAndReconciliationConsistency(IntegrationTestCase):
	def test_auto_apply_callback_requires_apply_payment_mode(self):
		doc = _new_settings(auto_apply_callback_payments=1, inbound_mode="Log Only")
		with self.assertRaises(frappe.ValidationError):
			doc._validate_inbound_consistency()

	def test_auto_apply_reconciliation_requires_reconciliation_enabled(self):
		doc = _new_settings(auto_apply_reconciliation_payments=1, reconciliation_enabled=0)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_reconciliation_consistency()

	def test_reconciliation_lookback_days_must_be_positive(self):
		doc = _new_settings(reconciliation_enabled=1, reconciliation_lookback_days=0)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_reconciliation_consistency()


class TestValidateTimeouts(IntegrationTestCase):
	def test_rejects_non_positive_connect_timeout(self):
		doc = _new_settings(connect_timeout_seconds=0, read_timeout_seconds=15)
		with self.assertRaises(frappe.ValidationError):
			doc._validate_timeouts()

	def test_accepts_positive_timeouts(self):
		doc = _new_settings(connect_timeout_seconds=5, read_timeout_seconds=15)
		doc._validate_timeouts()  # must not raise


class TestOnload(IntegrationTestCase):
	def test_onload_derives_ipn_url_from_site(self):
		doc = _new_settings()
		with patch(f"{MODULE}.get_url", return_value="https://site.example.com/api/method/x"):
			doc.onload()
		self.assertEqual(doc.ipn_callback_url, "https://site.example.com/api/method/x")

	def test_onload_falls_back_to_relative_path_on_error(self):
		doc = _new_settings()
		with patch(f"{MODULE}.get_url", side_effect=Exception("no request context")):
			doc.onload()
		self.assertEqual(doc.ipn_callback_url, "/api/method/landms.api.tcb.ipn_callback")
