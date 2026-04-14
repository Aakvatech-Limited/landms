from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from landms import tcb


class TestDuplicateIPNGuard(FrappeTestCase):
	@patch("landms.tcb.frappe.db.exists")
	def test_failed_ipn_log_does_not_block_retry(self, mock_exists):
		tcb.has_duplicate_ipn("048-503-DDE7Y0JNV0", "9991145330056")

		mock_exists.assert_called_once_with(
			"TCB API Log",
			{
				"direction": "Inbound",
				"event_type": "IPN Callback",
				"transaction_id": "048-503-DDE7Y0JNV0",
				"external_reference": "9991145330056",
				"status": ("!=", "Failed"),
			},
		)
