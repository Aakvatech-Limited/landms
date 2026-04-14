import frappe
from frappe.tests.utils import FrappeTestCase

from landms.sales_order_hooks import (
	_build_contract_schedule_rows,
	_draft_contract_matches_sales_order,
)


class TestDraftPlotContractSync(FrappeTestCase):
	def _make_source_doc(self, *, control_number="9991145330056", payment_amounts=None):
		payment_amounts = payment_amounts or [200.0, 800.0]
		return frappe._dict({
			"control_number": control_number,
			"plot_application": "PAPP-0001",
			"payment_deadline": "2026-04-30",
			"payment_schedule": [
				frappe._dict({
					"description": "Advance" if idx == 1 else "Balance",
					"due_date": "2026-04-14" if idx == 1 else "2026-04-30",
					"payment_amount": amount,
				})
				for idx, amount in enumerate(payment_amounts, start=1)
			],
		})

	def _make_contract_doc(self, source_doc):
		contract = frappe.new_doc("Plot Contract")
		contract.control_number = source_doc.control_number
		contract.plot_application = source_doc.plot_application
		contract.payment_deadline = source_doc.payment_deadline
		contract.set("payment_schedule", [])
		for row in _build_contract_schedule_rows(source_doc):
			contract.append("payment_schedule", row)
		return contract

	def test_draft_contract_matches_when_already_synced(self):
		source_doc = self._make_source_doc()
		contract = self._make_contract_doc(source_doc)

		self.assertTrue(_draft_contract_matches_sales_order(contract, source_doc))

	def test_draft_contract_detects_header_change(self):
		source_doc = self._make_source_doc()
		contract = self._make_contract_doc(source_doc)
		contract.control_number = "9991145330099"

		self.assertFalse(_draft_contract_matches_sales_order(contract, source_doc))

	def test_draft_contract_detects_schedule_change(self):
		source_doc = self._make_source_doc()
		contract = self._make_contract_doc(source_doc)
		contract.payment_schedule[0].expected_amount = 300

		self.assertFalse(_draft_contract_matches_sales_order(contract, source_doc))
