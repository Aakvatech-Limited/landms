from unittest.mock import MagicMock, call, patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import tasks


class TestApplicationAndContractDeadlines(FrappeTestCase):
	@patch("landms.tasks.frappe.db.sql")
	@patch("landms.tasks.frappe.get_single")
	def test_paid_application_expiry_does_not_depend_on_checkbox(self, mock_get_single, mock_sql):
		mock_get_single.return_value = frappe._dict(application_fee_validity_days=7)

		def _assert_query(query, *args, **kwargs):
			self.assertNotIn("apply_auto_cancellation", query)
			return []

		mock_sql.side_effect = _assert_query

		tasks.auto_expire_paid_applications_past_deadline()

		mock_sql.assert_called_once()

	@patch("landms.tasks.frappe.logger")
	@patch("landms.tasks.frappe.db.commit")
	@patch("landms.tasks.frappe.db.set_value")
	@patch("landms.tasks.frappe.db.get_value")
	@patch("landms.tasks.frappe.db.exists")
	@patch("landms.tasks.frappe.db.get_all")
	def test_overdue_contract_waits_for_manual_action_when_auto_cancel_off(
		self,
		mock_get_all,
		mock_exists,
		mock_get_value,
		mock_set_value,
		mock_commit,
		mock_logger,
	):
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "CTR-TEST-0001",
					"sales_order": "SAL-ORD-TEST-0001",
					"plot": "PLT-TEST-0001",
					"customer": "Ekta",
					"contract_status": "Ongoing",
					"apply_auto_cancellation": 1,
					"payment_deadline": "2026-04-01",
				}
			)
		]
		mock_exists.return_value = True
		mock_get_value.return_value = 0
		mock_logger.return_value = MagicMock()

		tasks.auto_process_overdue_plot_contracts()

		mock_set_value.assert_has_calls(
			[
				call("Plot Contract", "CTR-TEST-0001", "apply_auto_cancellation", 0, update_modified=False),
				call("Plot Contract", "CTR-TEST-0001", "contract_status", "Overdue", update_modified=True),
			]
		)
		mock_commit.assert_called_once()

	@patch("landms.tasks.frappe.logger")
	@patch("landms.tasks.frappe.db.commit")
	@patch("landms.tasks.frappe.db.set_value")
	@patch("landms.tasks.frappe.get_doc")
	@patch("landms.tasks.frappe.db.get_value")
	@patch("landms.tasks.frappe.db.exists")
	@patch("landms.tasks.frappe.db.get_all")
	def test_overdue_contract_auto_terminates_when_enabled(
		self,
		mock_get_all,
		mock_exists,
		mock_get_value,
		mock_get_doc,
		mock_set_value,
		mock_commit,
		mock_logger,
	):
		mock_get_all.return_value = [
			frappe._dict(
				{
					"name": "CTR-TEST-0002",
					"sales_order": "SAL-ORD-TEST-0002",
					"plot": "PLT-TEST-0002",
					"customer": "Innocent",
					"contract_status": "Ongoing",
					"apply_auto_cancellation": 0,
					"payment_deadline": "2026-04-01",
				}
			)
		]
		mock_exists.return_value = True
		mock_get_value.return_value = 1
		mock_logger.return_value = MagicMock()

		contract = MagicMock()
		contract.name = "CTR-TEST-0002"
		contract.contract_status = "Ongoing"
		mock_get_doc.return_value = contract

		tasks.auto_process_overdue_plot_contracts()

		mock_set_value.assert_called_once_with(
			"Plot Contract",
			"CTR-TEST-0002",
			"apply_auto_cancellation",
			1,
			update_modified=False,
		)
		contract.db_set.assert_called_once_with("contract_status", "Overdue", update_modified=True)
		contract.terminate_contract.assert_called_once_with(
			"Payment deadline missed — auto-cancelled by scheduler."
		)
		mock_commit.assert_called_once()
