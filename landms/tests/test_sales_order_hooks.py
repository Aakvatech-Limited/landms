import frappe
from frappe.tests.utils import FrappeTestCase

from landms.sales_order_hooks import (
	_build_contract_schedule_rows,
	_draft_contract_matches_sales_order,
	_validate_customer_mobile_for_tcb,
)
from landms.tcb import is_valid_tcb_mobile


class TestDraftPlotContractSync(FrappeTestCase):
	def _make_source_doc(self, *, control_number="9991145330056", payment_amounts=None, apply_auto_cancellation=1):
		payment_amounts = payment_amounts or [200.0, 800.0]
		return frappe._dict({
			"control_number": control_number,
			"plot_application": "PAPP-0001",
			"payment_deadline": "2026-04-30",
			"apply_auto_cancellation": apply_auto_cancellation,
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
		contract.apply_auto_cancellation = source_doc.apply_auto_cancellation
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

	def test_draft_contract_detects_auto_cancel_change(self):
		source_doc = self._make_source_doc(apply_auto_cancellation=1)
		contract = self._make_contract_doc(source_doc)
		contract.apply_auto_cancellation = 0

		self.assertFalse(_draft_contract_matches_sales_order(contract, source_doc))

	def test_draft_contract_detects_schedule_change(self):
		source_doc = self._make_source_doc()
		contract = self._make_contract_doc(source_doc)
		contract.payment_schedule[0].expected_amount = 300

		self.assertFalse(_draft_contract_matches_sales_order(contract, source_doc))


class TestTcbCustomerMobileValidation(FrappeTestCase):
	def test_tcb_mobile_validator_accepts_required_format(self):
		self.assertTrue(is_valid_tcb_mobile("255762064315"))
		self.assertFalse(is_valid_tcb_mobile("0762064315"))
		self.assertFalse(is_valid_tcb_mobile("+255762064315"))
		self.assertFalse(is_valid_tcb_mobile("25576206431"))

	def test_validate_customer_mobile_for_tcb_throws_for_invalid_customer_mobile(self):
		doc = frappe._dict({"customer": "CUST-0001"})
		original_get_value = frappe.db.get_value
		frappe.db.get_value = lambda doctype, name, fieldname=None, *args, **kwargs: "0762064315"
		try:
			with self.assertRaises(frappe.ValidationError):
				_validate_customer_mobile_for_tcb(doc)
		finally:
			frappe.db.get_value = original_get_value

	def test_validate_customer_mobile_for_tcb_accepts_valid_customer_mobile(self):
		doc = frappe._dict({"customer": "CUST-0001"})
		original_get_value = frappe.db.get_value
		frappe.db.get_value = lambda doctype, name, fieldname=None, *args, **kwargs: "255762064315"
		try:
			_validate_customer_mobile_for_tcb(doc)
		finally:
			frappe.db.get_value = original_get_value
