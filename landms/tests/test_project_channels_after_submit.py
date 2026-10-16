"""A submitted project's collection channels can be ticked again.

Operator, 14 Oct 2026: the live project is submitted, so "Collect via Malipopay"
could not be ticked at go-live. The two channel boxes are now allowed after
submit; the issuer stays locked and must stay ticked; the cost figures and the
period summary the form rebuilds on opening do not block the save. Operator,
16 Oct 2026: only a System Manager may change the Malipopay box, and it cannot
be unticked while the project has a Malipopay bill that is not ended. Every
write is rolled back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt


def _boxes_allowed_after_submit():
	# A site migrated before this change still has the boxes locked in its
	# metadata; give this test process the new definition, in memory only.
	meta = frappe.get_meta("Land Acquisition")
	for fieldname in ("collect_via_tcb", "collect_via_malipopay"):
		meta.get_field(fieldname).allow_on_submit = 1


class TestProjectChannelsAfterSubmit(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		name = frappe.db.get_value(
			"Land Acquisition",
			{"docstatus": 1, "payment_channel": "TCB", "collect_via_tcb": 1},
			"name",
		)
		if not name:
			self.skipTest("this site has no submitted project issuing through TCB")
		_boxes_allowed_after_submit()
		self.name = name

	def _as_the_form_sends(self):
		doc = frappe.get_doc("Land Acquisition", self.name)
		doc.cost_per_sqm_tzs = flt(doc.cost_per_sqm_tzs) + 3e-12  # the browser's extra decimals
		doc.payment_period_summary = "rebuilt by the form"
		return doc

	def _stored(self, *fields):
		return frappe.db.get_value("Land Acquisition", self.name, list(fields), as_dict=True)

	def test_malipopay_can_be_ticked_and_the_figures_stay_as_stored(self):
		before = self._stored("cost_per_sqm_tzs", "payment_period_summary")
		doc = self._as_the_form_sends()
		doc.collect_via_malipopay = 0 if doc.collect_via_malipopay else 1
		wanted = doc.collect_via_malipopay
		doc.save()
		after = self._stored(
			"collect_via_malipopay", "payment_channel", "cost_per_sqm_tzs", "payment_period_summary"
		)
		self.assertEqual(after.collect_via_malipopay, wanted)
		self.assertEqual(after.payment_channel, "TCB")
		self.assertEqual(after.cost_per_sqm_tzs, before.cost_per_sqm_tzs)
		self.assertEqual(after.payment_period_summary, before.payment_period_summary)

	def test_the_issuing_channel_cannot_be_unticked(self):
		doc = self._as_the_form_sends()
		doc.collect_via_malipopay = 1
		doc.collect_via_tcb = 0
		with self.assertRaisesRegex(frappe.ValidationError, "must stay ticked"):
			doc.save()

	def test_the_issuer_itself_stays_locked(self):
		doc = self._as_the_form_sends()
		doc.collect_via_malipopay = 1
		doc.payment_channel = "Malipopay"
		with self.assertRaises(frappe.ValidationError):
			doc.save()
		self.assertEqual(self._stored("payment_channel").payment_channel, "TCB")

	def test_a_real_change_to_a_locked_field_is_still_refused(self):
		doc = self._as_the_form_sends()
		doc.total_area_sqm = flt(doc.total_area_sqm) + 5
		with self.assertRaises(frappe.UpdateAfterSubmitError):
			doc.save()

	def _malipopay_ticked(self):
		doc = self._as_the_form_sends()
		if not doc.collect_via_malipopay:
			doc.collect_via_malipopay = 1
			doc.save()
		return self._as_the_form_sends()

	def _bill(self, state):
		order = frappe.db.get_value("Sales Order", {"land_acquisition": self.name, "docstatus": 1}, "name")
		if not order:
			self.skipTest("this project has no submitted sales order to hang a test bill on")
		return frappe.get_doc(
			{
				"doctype": "Malipopay Collection",
				"control_number": "GUARDTEST" + frappe.generate_hash(length=8),
				"state": state,
				"sales_order": order,
				"land_acquisition": self.name,
			}
		).insert(ignore_permissions=True)

	def test_malipopay_cannot_be_unticked_while_a_bill_is_open(self):
		doc = self._malipopay_ticked()
		self._bill("Active")
		doc.collect_via_malipopay = 0
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be switched off"):
			doc.save()
		self.assertEqual(self._stored("collect_via_malipopay").collect_via_malipopay, 1)

	def test_malipopay_can_be_unticked_when_no_bill_is_open(self):
		doc = self._malipopay_ticked()
		doc.collect_via_malipopay = 0
		with patch.object(type(doc), "_open_malipopay_bills", return_value=0):
			doc.save()
		self.assertEqual(self._stored("collect_via_malipopay").collect_via_malipopay, 0)

	def test_ended_bills_are_not_counted_as_open(self):
		doc = frappe.get_doc("Land Acquisition", self.name)
		start = doc._open_malipopay_bills()
		self._bill("Dead")
		self.assertEqual(doc._open_malipopay_bills(), start)
		self._bill("Suspended")
		self.assertEqual(doc._open_malipopay_bills(), start + 1)

	def test_only_a_system_manager_can_change_the_malipopay_box(self):
		doc = self._as_the_form_sends()
		doc.collect_via_malipopay = 0 if doc.collect_via_malipopay else 1
		doc.flags.ignore_permissions = True  # so only the guard can refuse
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user("Guest")
		with self.assertRaisesRegex(frappe.PermissionError, "Only a System Manager"):
			doc.save()

	def test_other_changes_by_anyone_are_not_blocked_by_the_guard(self):
		doc = self._as_the_form_sends()
		doc.flags.ignore_permissions = True
		self.addCleanup(frappe.set_user, frappe.session.user)
		frappe.set_user("Guest")
		doc.save()  # the box is unchanged, so the guard does not apply

	def test_the_boxes_are_allowed_after_submit_in_the_doctype(self):
		import json
		import os

		path = os.path.join(
			frappe.get_app_path("landms", "landms", "doctype", "land_acquisition"), "land_acquisition.json"
		)
		fields = {f["fieldname"]: f for f in json.load(open(path))["fields"]}
		self.assertEqual(fields["collect_via_tcb"].get("allow_on_submit"), 1)
		self.assertEqual(fields["collect_via_malipopay"].get("allow_on_submit"), 1)
		self.assertFalse(fields["payment_channel"].get("allow_on_submit"))
