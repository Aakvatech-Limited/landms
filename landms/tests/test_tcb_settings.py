from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import tcb


class TestTCBSettingsBackwardCompatibility(FrappeTestCase):
	@patch("landms.tcb._get_tcb_integration_doc")
	def test_missing_optional_single_fields_do_not_crash(self, mock_get_settings_doc):
		mock_get_settings_doc.return_value = frappe._dict({
			"enabled": 1,
			"outbound_mode": "Off",
			"inbound_mode": "Off",
			"control_number_pattern": "99911####00##",
			"auto_apply_callback_payments": 0,
			"auto_apply_reconciliation_payments": 0,
			"reconciliation_enabled": 1,
			"reconciliation_lookback_days": 3,
			"decline_reference_on_so_cancel": 1,
			"decline_failure_policy": "Allow Cancel and Flag",
			"reference_create_url": "",
			"reference_decline_url": "",
			"reconciliation_url": "https://example.com/recon",
			"partner_code": "PARTNER",
			"profile_id": "PROFILE",
			"verify_ssl": 1,
			"connect_timeout_seconds": 5,
			"read_timeout_seconds": 15,
		})

		settings = tcb._get_tcb_settings()

		self.assertEqual(settings["reconciliation_url"], "https://example.com/recon")
