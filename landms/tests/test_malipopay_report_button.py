"""The "Report to Malipopay" button on a Malipopay bill.

It runs the 05:10 external-entries pass for one bill, so a message Malipopay
refused or never answered can be resent by whoever is in the office. The pass
itself is faked here; the test checks who may press it and what they are told.
Every write rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase


class TestReportToMalipopay(FrappeTestCase):
	def setUp(self):
		self.logs = []
		self.result = frappe._dict(sent=[], planned=[], message="")
		self._orig_user = frappe.session.user
		frappe.set_user("Administrator")
		for target, kwargs in (
			(
				"landms.malipopay_external.sync_external_entries",
				{"side_effect": lambda name, trigger: self.result},
			),
			("landms.malipopay.create_malipopay_api_log", {"side_effect": lambda **kw: self.logs.append(kw)}),
		):
			p = patch(target, **kwargs)
			p.start()
			self.addCleanup(p.stop)
		p = patch.object(frappe.db, "commit", lambda *a, **k: None)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)
		self.addCleanup(lambda: frappe.set_user(self._orig_user))
		self.bill = self._insert(
			"Malipopay Collection",
			control_number="TESTMPREPORT1",
			sales_order="SAL-ORD-TEST-REPORT",
			state="Active",
			ml_reference="MLTESTREPORT1",
		)

	def _insert(self, doctype, **values):
		doc = frappe.get_doc(dict(doctype=doctype, **values))
		doc.flags.ignore_links = True
		doc.flags.ignore_mandatory = True
		doc.insert(ignore_permissions=True)
		return doc

	def _entry(self, status, code=None, message=None):
		return self._insert(
			"Malipopay External Entry",
			status=status,
			entry_type="CREDIT",
			amount=100,
			external_reference="TESTPE-REPORT-C1",
			malipopay_collection=self.bill.name,
			payment_entry="TESTPE-REPORT",
			provider_code=code,
			message=message,
		).name

	def test_bill_without_reference_has_nothing_to_report(self):
		self.bill.db_set("ml_reference", None)
		out = self.bill.report_to_malipopay()
		self.assertFalse(out["ok"])
		self.assertIn("no Malipopay reference", out["message"])

	def test_nothing_missing_says_so(self):
		self.result.message = "in step"
		out = self.bill.report_to_malipopay()
		self.assertTrue(out["ok"])
		self.assertIn("nothing to send", out["message"])

	def test_sent_message_is_listed(self):
		self.result.sent = [(self._entry("Sent", "3150", "posted"), "Sent")]
		self.result.message = "sent 1 of 1"
		out = self.bill.report_to_malipopay()
		self.assertTrue(out["ok"])
		self.assertIn("CREDIT 100 for payment TESTPE-REPORT: Sent", out["message"])

	def test_refusal_shows_malipopay_answer(self):
		self.result.sent = [(self._entry("Refused", "3154", "not enabled"), "Refused")]
		out = self.bill.report_to_malipopay()
		self.assertFalse(out["ok"])
		self.assertIn("3154 not enabled", out["message"])

	def test_pass_that_sent_nothing_gives_its_reason(self):
		self.result.message = "breakdown fetch failed — nothing planned"
		out = self.bill.report_to_malipopay()
		self.assertFalse(out["ok"])
		self.assertIn("breakdown fetch failed", out["message"])

	def test_every_press_is_logged(self):
		self.result.message = "in step"
		self.bill.report_to_malipopay()
		self.assertEqual(self.logs[-1]["event_type"], "External Receipt")
		self.assertEqual(self.logs[-1]["processing_mode"], "Form Action")

	def test_only_managers_may_press_it(self):
		frappe.set_user("Guest")
		# frappe.only_for lets everyone through while tests run; switch that off
		with patch.dict(frappe.local.flags, {"in_test": False}), self.assertRaises(frappe.PermissionError):
			self.bill.report_to_malipopay()
