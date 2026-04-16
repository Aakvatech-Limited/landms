from unittest.mock import MagicMock, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import tcb
from landms.landms.doctype.tcb_reconciliation_log import tcb_reconciliation_log


class TestReconciliationStopControl(FrappeTestCase):
	@patch("landms.tcb.create_tcb_api_log")
	@patch("landms.landms.doctype.tcb_reconciliation_log.tcb_reconciliation_log.frappe.db.commit")
	@patch("landms.landms.doctype.tcb_reconciliation_log.tcb_reconciliation_log.frappe.get_doc")
	def test_request_stop_marks_flag_and_logs(self, mock_get_doc, mock_commit, mock_create_log):
		doc = MagicMock()
		doc.status = "Running"
		doc.stop_requested = 0
		doc.name = "TCB-RECON-2026-000010"
		doc._request_stop = tcb_reconciliation_log.TCBReconciliationLog._request_stop.__get__(
			doc, tcb_reconciliation_log.TCBReconciliationLog
		)
		mock_get_doc.return_value = doc

		with patch(
			"landms.landms.doctype.tcb_reconciliation_log.tcb_reconciliation_log.frappe.session",
			frappe._dict(user="qa@example.com"),
		):
			result = tcb_reconciliation_log.request_stop("TCB-RECON-2026-000010")

		self.assertTrue(result["ok"])
		doc.db_set.assert_called_once()
		mock_commit.assert_called_once()
		mock_create_log.assert_called_once()

	@patch("landms.tcb.create_tcb_api_log")
	@patch("landms.tcb._update_recon_log")
	@patch("landms.tcb._is_reconciliation_stop_requested", return_value=True)
	def test_reconciliation_job_stops_when_requested(self, mock_stop, mock_update, mock_create_log):
		result = tcb._stop_reconciliation_if_requested(
			log_name="TCB-RECON-2026-000011",
			settings={"reconciliation_url": "https://example.com/recon"},
			started_at=0.0,
			total_rows=3,
			applied=1,
			ignored=1,
			failed=1,
		)

		self.assertEqual(result["status"], "Stopped")
		mock_update.assert_called_once()
		mock_create_log.assert_called_once()
