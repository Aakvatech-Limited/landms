from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from landms.journal_entry_hooks import before_save_journal_entry


class TestBeforeSaveJournalEntry(IntegrationTestCase):
	def _make_doc(self, rows):
		doc = frappe._dict(accounts=[frappe._dict(row) for row in rows])
		return doc

	def test_fills_cost_center_on_rows_with_land_acquisition(self):
		doc = self._make_doc(
			[
				{"land_acquisition": "LACQ-TEST-0001", "cost_center": None},
				{"land_acquisition": "", "cost_center": None},
			]
		)
		with patch(
			"landms.journal_entry_hooks.frappe.db.get_single_value",
			return_value="Land Acquisition - LA",
		):
			before_save_journal_entry(doc)

		self.assertEqual(doc.accounts[0].cost_center, "Land Acquisition - LA")
		self.assertIsNone(doc.accounts[1].cost_center)

	def test_does_nothing_when_settings_cost_center_is_unset(self):
		doc = self._make_doc([{"land_acquisition": "LACQ-TEST-0001", "cost_center": None}])
		with patch("landms.journal_entry_hooks.frappe.db.get_single_value", return_value=None):
			before_save_journal_entry(doc)

		self.assertIsNone(doc.accounts[0].cost_center)

	def test_handles_no_accounts_rows(self):
		doc = frappe._dict(accounts=None)
		with patch(
			"landms.journal_entry_hooks.frappe.db.get_single_value",
			return_value="Land Acquisition - LA",
		):
			before_save_journal_entry(doc)  # must not raise
