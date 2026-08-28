import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import flt

from landms.payment_sync import _build_pe_references_for_invoice
from landms.sales_order_hooks import (
	_build_contract_schedule_rows,
	_draft_contract_matches_sales_order,
	_validate_customer_mobile_for_tcb,
	build_payment_schedule_rows,
)
from landms.tcb import is_valid_tcb_mobile


class TestDraftPlotContractSync(IntegrationTestCase):
	def _make_source_doc(
		self, *, control_number="9991145330056", payment_amounts=None, apply_auto_cancellation=1
	):
		payment_amounts = payment_amounts or [200.0, 800.0]
		return frappe._dict(
			{
				"control_number": control_number,
				"plot_application": "PAPP-0001",
				"payment_deadline": "2026-04-30",
				"apply_auto_cancellation": apply_auto_cancellation,
				"payment_schedule": [
					frappe._dict(
						{
							"description": "Advance" if idx == 1 else "Balance",
							"due_date": "2026-04-14" if idx == 1 else "2026-04-30",
							"payment_amount": amount,
						}
					)
					for idx, amount in enumerate(payment_amounts, start=1)
				],
			}
		)

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


class TestTcbCustomerMobileValidation(IntegrationTestCase):
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


class TestLandMsPaymentScheduleTerms(IntegrationTestCase):
	def test_schedule_rows_include_named_payment_terms(self):
		rows = build_payment_schedule_rows(
			total_amount=1000,
			booking_fee_percent=10,
			transaction_date="2026-04-17",
			payment_deadline="2026-05-17",
		)

		self.assertEqual(len(rows), 2)
		self.assertEqual(rows[0]["payment_term"], "Advance")
		self.assertEqual(rows[1]["payment_term"], "Balance")

	def test_single_row_schedule_keeps_advance_term(self):
		rows = build_payment_schedule_rows(
			total_amount=1000,
			booking_fee_percent=0,
			transaction_date="2026-04-17",
			payment_deadline="2026-04-17",
		)

		self.assertEqual(len(rows), 1)
		self.assertEqual(rows[0]["payment_term"], "Advance")

	def test_pe_references_allocate_to_invoice_terms_in_order(self):
		si = frappe._dict(
			{
				"name": "ACC-SINV-TEST-0001",
				"payment_schedule": [
					frappe._dict(
						{
							"idx": 1,
							"payment_term": "Advance",
							"outstanding": 0.3,
						}
					),
					frappe._dict(
						{
							"idx": 2,
							"payment_term": "Balance",
							"outstanding": 2.7,
						}
					),
				],
			}
		)

		refs = _build_pe_references_for_invoice(si, 1.0)

		self.assertEqual(len(refs), 2)
		self.assertEqual(refs[0]["payment_term"], "Advance")
		self.assertEqual(flt(refs[0]["allocated_amount"]), 0.3)
		self.assertEqual(refs[1]["payment_term"], "Balance")
		self.assertEqual(flt(refs[1]["allocated_amount"]), 0.7)

	def test_pe_references_fall_back_when_invoice_has_no_schedule(self):
		si = frappe._dict(
			{
				"name": "ACC-SINV-TEST-0002",
				"payment_schedule": [],
			}
		)

		refs = _build_pe_references_for_invoice(si, 2.5)

		self.assertEqual(
			refs,
			[
				{
					"reference_doctype": "Sales Invoice",
					"reference_name": "ACC-SINV-TEST-0002",
					"allocated_amount": 2.5,
				}
			],
		)
