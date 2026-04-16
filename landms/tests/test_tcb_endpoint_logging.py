from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms.api import tcb as api_tcb
from landms.landms.doctype.tcb_control_number.tcb_control_number import TCBControlNumber
from landms.landms.doctype.tcb_reconciliation_log import tcb_reconciliation_log


class TestTCBEndpointLogging(FrappeTestCase):
	@patch("landms.api.tcb.create_tcb_api_log")
	@patch("landms.api.tcb.frappe.get_roles", return_value=["Accounts User"])
	def test_manual_reconciliation_permission_denied_is_logged(self, mock_roles, mock_create_log):
		with patch("landms.api.tcb.frappe.session", frappe._dict(user="qa@example.com")):
			with self.assertRaises(frappe.ValidationError):
				api_tcb.run_reconciliation("2026-04-01", "2026-04-14")

		mock_create_log.assert_called_once()
		self.assertEqual(mock_create_log.call_args.kwargs["event_type"], "Reconciliation")
		self.assertEqual(mock_create_log.call_args.kwargs["status"], "Failed")

	@patch("landms.api.tcb.create_tcb_api_log")
	@patch("landms.api.tcb.run_tcb_reconciliation_job", return_value={"ok": True, "status": "Success", "message": "Done"})
	@patch("landms.api.tcb.frappe.get_roles", return_value=["System Manager"])
	def test_manual_reconciliation_success_is_logged(self, mock_roles, mock_run, mock_create_log):
		with patch("landms.api.tcb.frappe.session", frappe._dict(user="qa@example.com")):
			result = api_tcb.run_reconciliation("2026-04-01", "2026-04-14")

		self.assertTrue(result["ok"])
		mock_create_log.assert_called_once()
		self.assertEqual(mock_create_log.call_args.kwargs["event_type"], "Reconciliation")
		self.assertEqual(mock_create_log.call_args.kwargs["status"], "Success")

	@patch("landms.api.tcb.create_tcb_api_log")
	@patch("landms.api.tcb.get_tcb_inbound_mode", side_effect=RuntimeError("boom"))
	@patch("landms.api.tcb._extract_ipn_body", return_value={"reference": "9991145330056", "transaction_id": "048-503-DDE7Y0JNV0", "amount": 200})
	@patch("landms.api.tcb._read_request_payload", return_value=({"body": {"status": 0}}, {"param": {"reference": "9991145330056"}}))
	def test_ipn_unhandled_exception_is_logged(self, mock_read, mock_extract, mock_mode, mock_create_log):
		result = api_tcb.ipn_callback()

		self.assertFalse(result["ok"])
		self.assertEqual(result["status"], "Failed")
		mock_create_log.assert_called_once()
		self.assertEqual(mock_create_log.call_args.kwargs["event_type"], "IPN Callback")
		self.assertEqual(mock_create_log.call_args.kwargs["status"], "Failed")

	@patch("landms.tcb.create_tcb_api_log")
	def test_decline_control_number_invalid_status_is_logged(self, mock_create_log):
		doc = TCBControlNumber({
			"doctype": "TCB Control Number",
			"name": "9991145330056",
			"control_number": "9991145330056",
			"status": "Paid",
			"sales_order": "SAL-ORD-2026-00003",
		})

		with patch(
			"landms.landms.doctype.tcb_control_number.tcb_control_number.frappe.session",
			frappe._dict(user="qa@example.com"),
		):
			with self.assertRaises(frappe.ValidationError):
				doc.decline_control_number()

		mock_create_log.assert_called_once()
		self.assertEqual(mock_create_log.call_args.kwargs["event_type"], "Reference Decline")
		self.assertEqual(mock_create_log.call_args.kwargs["status"], "Failed")

	@patch("landms.tcb.create_tcb_api_log")
	@patch(
		"landms.landms.doctype.tcb_reconciliation_log.tcb_reconciliation_log.frappe.get_doc",
		side_effect=RuntimeError("missing"),
	)
	def test_reconciliation_run_endpoint_failure_is_logged(self, mock_get_doc, mock_create_log):
		with patch(
			"landms.landms.doctype.tcb_reconciliation_log.tcb_reconciliation_log.frappe.session",
			frappe._dict(user="qa@example.com"),
		):
			with self.assertRaises(RuntimeError):
				tcb_reconciliation_log.run("TCB-RECON-2026-000099")

		mock_create_log.assert_called_once()
		self.assertEqual(mock_create_log.call_args.kwargs["event_type"], "Reconciliation")
		self.assertEqual(mock_create_log.call_args.kwargs["status"], "Failed")
