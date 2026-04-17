from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestPlotContractPaymentSyncPersistence(FrappeTestCase):
	def test_persist_payment_sync_state_saves_draft_contract(self):
		contract = frappe.new_doc("Plot Contract")
		contract.docstatus = 0

		with patch("landms.landms.doctype.plot_contract.plot_contract.Document.save") as mock_save:
			contract._persist_payment_sync_state()

		mock_save.assert_called_once_with(ignore_permissions=True)

	def test_persist_payment_sync_state_updates_submitted_contract_via_db(self):
		contract = frappe.new_doc("Plot Contract")
		contract.name = "CTR-TEST-0001"
		contract.docstatus = 1
		contract.plot_application = "PA-TEST-0001"
		contract.control_number = "9991140750085"
		contract.booking_fee_invoice = "ACC-SINV-TEST-0001"
		contract.payment_deadline = "2026-04-27"
		contract.total_contract_value = 700
		contract.total_paid = 70
		contract.total_outstanding = 630
		contract.payment_progress = "Advance Paid"
		contract.contract_status = "Ongoing"

		with patch("landms.landms.doctype.plot_contract.plot_contract.frappe.db.set_value") as mock_set_value, patch(
			"landms.landms.doctype.plot_contract.plot_contract.frappe.clear_document_cache"
		) as mock_clear_cache:
			contract._persist_payment_sync_state()

		mock_set_value.assert_called_once_with(
			"Plot Contract",
			"CTR-TEST-0001",
			{
				"plot_application": "PA-TEST-0001",
				"control_number": "9991140750085",
				"booking_fee_invoice": "ACC-SINV-TEST-0001",
				"payment_deadline": "2026-04-27",
				"total_contract_value": 700.0,
				"total_paid": 70.0,
				"total_outstanding": 630.0,
				"payment_progress": "Advance Paid",
				"contract_status": "Ongoing",
			},
			update_modified=True,
		)
		mock_clear_cache.assert_called_once_with("Plot Contract", "CTR-TEST-0001")
