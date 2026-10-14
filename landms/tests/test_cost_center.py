"""Every LandMS document takes its cost centre from LandMS Settings.

The builders set it on the lines but never on the headers; when the live site
made the headers mandatory (Customize Form, 6-7 Sep 2026), plot sales and
TCB payments broke and five "Plot cost center" Server Scripts were added as
guards. landms.cost_center does their job inside the app: empty fields only,
LandMS documents of the LandMS company only, and it never raises.
Every write is rolled back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms.cost_center import apply_landms_cost_center

WIRED = ("Sales Order", "Sales Invoice", "Payment Entry", "Delivery Note", "Journal Entry")


class TestLandmsCostCentre(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		settings = frappe.get_cached_doc("LandMS Settings")
		self.company = settings.company
		self.cost_center = settings.cost_center
		if not (self.company and self.cost_center):
			self.skipTest("LandMS Settings has no company or cost centre on this site")
		self.company_default = frappe.get_cached_value("Company", self.company, "cost_center")
		self.plot_so = frappe.db.get_value(
			"Sales Order", {"docstatus": 1, "plot": ["is", "set"]}, "name", order_by="creation desc"
		)
		self.plot_si = frappe.db.get_value(
			"Sales Invoice", {"docstatus": 1, "is_plot_sale_invoice": 1}, "name", order_by="creation desc"
		)
		if not (self.plot_so and self.plot_si):
			self.skipTest("this site has no plot Sales Order / Sales Invoice to point at")

	def _doc(self, doctype, **values):
		return frappe.get_doc(dict({"doctype": doctype, "company": self.company}, **values))

	def _chosen_cost_centre(self):
		"""A cost centre someone picked on purpose (neither the setting nor the
		company default)."""
		parent = frappe.db.get_value("Cost Center", {"company": self.company, "is_group": 1}, "name")
		doc = frappe.get_doc(
			{
				"doctype": "Cost Center",
				"cost_center_name": "TEST Chosen By Hand",
				"company": self.company,
				"parent_cost_center": parent,
				"is_group": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	# ------------------------------------------------------------------ the wiring

	def test_the_rule_runs_before_validation_on_all_five_doctypes(self):
		events = frappe.get_hooks("doc_events")
		for doctype in WIRED:
			self.assertIn(
				"landms.cost_center.apply_landms_cost_center",
				events.get(doctype, {}).get("before_validate", []),
				doctype,
			)

	def test_plot_order_defaults_carry_the_cost_centre_to_the_form(self):
		from landms.sales_order_hooks import get_sales_order_defaults

		app = frappe.db.get_value("Plot Application", {"docstatus": 1, "plot": ["is", "set"]}, "name")
		if not app:
			self.skipTest("this site has no submitted Plot Application")
		frappe.db.set_value("Plot Application", app, "status", "Paid", update_modified=False)
		self.assertEqual(get_sales_order_defaults(app)["cost_center"], self.cost_center)

	# ------------------------------------------------------------------ LandMS documents get it

	def test_a_plot_sales_order_gets_the_setting_on_its_header_and_rows(self):
		doc = self._doc(
			"Sales Order", plot="PLT-TEST", items=[{"item_code": "X", "cost_center": self.company_default}]
		)
		apply_landms_cost_center(doc)
		self.assertEqual(doc.cost_center, self.cost_center)
		self.assertEqual(doc.items[0].cost_center, self.cost_center)  # company default = unset

	def test_a_plot_sales_invoice_and_its_credit_note_get_the_setting(self):
		invoice = self._doc("Sales Invoice", is_plot_sale_invoice=1, items=[{"item_code": "X"}])
		apply_landms_cost_center(invoice)
		self.assertEqual((invoice.cost_center, invoice.items[0].cost_center), (self.cost_center,) * 2)
		credit_note = self._doc("Sales Invoice", is_return=1, return_against=self.plot_si, items=[{}])
		apply_landms_cost_center(credit_note)
		self.assertEqual(credit_note.cost_center, self.cost_center)

	def test_an_invoice_made_from_a_plot_order_gets_the_setting(self):
		invoice = self._doc("Sales Invoice", items=[{"item_code": "X", "sales_order": self.plot_so}])
		apply_landms_cost_center(invoice)
		self.assertEqual(invoice.cost_center, self.cost_center)

	def test_a_payment_against_a_plot_gets_the_setting(self):
		by_project = self._doc("Payment Entry", land_acquisition="LACQ-TEST")
		apply_landms_cost_center(by_project)
		self.assertEqual(by_project.cost_center, self.cost_center)
		by_invoice = self._doc(
			"Payment Entry",
			references=[{"reference_doctype": "Sales Invoice", "reference_name": self.plot_si}],
		)
		apply_landms_cost_center(by_invoice)
		self.assertEqual(by_invoice.cost_center, self.cost_center)
		by_order = self._doc(
			"Payment Entry", references=[{"reference_doctype": "Sales Order", "reference_name": self.plot_so}]
		)
		apply_landms_cost_center(by_order)
		self.assertEqual(by_order.cost_center, self.cost_center)

	def test_a_handover_delivery_note_gets_the_setting(self):
		doc = self._doc("Delivery Note", items=[{"item_code": "X", "against_sales_order": self.plot_so}])
		apply_landms_cost_center(doc)
		self.assertEqual((doc.cost_center, doc.items[0].cost_center), (self.cost_center,) * 2)

	def test_a_landms_journal_entry_gets_the_setting_on_its_header_field_and_project_rows(self):
		doc = self._doc(
			"Journal Entry",
			lms_payment_entry="PE-TEST",
			accounts=[
				{"account": "A", "land_acquisition": "LACQ-TEST", "cost_center": self.company_default},
				{"account": "B"},
			],
		)
		apply_landms_cost_center(doc)
		self.assertEqual(doc.accounts[0].cost_center, self.cost_center)
		self.assertFalse(doc.accounts[1].cost_center)  # a row without a project is left alone
		if doc.meta.has_field("custom_cost_center"):  # the live site's own header field
			self.assertEqual(doc.custom_cost_center, self.cost_center)

	# ------------------------------------------------------------------ everything else is left alone

	def test_a_cost_centre_chosen_by_hand_is_kept(self):
		chosen = self._chosen_cost_centre()
		doc = self._doc("Sales Order", plot="PLT-TEST", cost_center=chosen, items=[{"cost_center": chosen}])
		apply_landms_cost_center(doc)
		self.assertEqual((doc.cost_center, doc.items[0].cost_center), (chosen, chosen))

	def test_documents_that_are_not_landms_are_untouched(self):
		for doctype, values in (
			("Sales Order", {"items": [{"item_code": "X"}]}),
			("Sales Invoice", {"items": [{"item_code": "X"}]}),
			("Payment Entry", {}),
			("Delivery Note", {"items": [{"item_code": "X"}]}),
			("Journal Entry", {"accounts": [{"account": "A"}]}),
		):
			doc = self._doc(doctype, **values)
			apply_landms_cost_center(doc)
			self.assertFalse(doc.get("cost_center"), doctype)
			self.assertFalse(doc.get("custom_cost_center"), doctype)

	def test_another_company_on_the_site_is_untouched(self):
		doc = self._doc("Sales Order", plot="PLT-TEST")
		doc.company = "Some Other Company"
		apply_landms_cost_center(doc)
		self.assertFalse(doc.cost_center)

	def test_without_a_cost_centre_in_the_settings_nothing_changes(self):
		settings = frappe._dict(company=self.company, cost_center=None)
		with patch("landms.cost_center.frappe.get_cached_doc", return_value=settings):
			doc = self._doc("Sales Order", plot="PLT-TEST")
			apply_landms_cost_center(doc)
		self.assertFalse(doc.cost_center)

	def test_a_failure_never_blocks_the_document(self):
		with (
			patch("landms.cost_center.frappe.get_cached_doc", side_effect=RuntimeError("boom")),
			patch("frappe.log_error") as logged,
		):
			doc = self._doc("Sales Order", plot="PLT-TEST")
			apply_landms_cost_center(doc)  # must not raise
		logged.assert_called_once()
		self.assertFalse(doc.cost_center)
