"""TCB reconciliation by project.

Each statement row is given to a project from LandMS's own records (the order
or registry entry carrying its control number, else the one project whose
numbering pattern fits), never from what TCB shares between projects. And one
account TCB does not answer for no longer stops every other project's
reconciliation (15 Oct 2026: TCB timed out for one account and no project was
reconciled). Bank calls and log writes are faked; nothing is saved.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import tcb


def _project(name, account, *patterns):
	return frappe._dict(
		name=name, label=name, account=account, patterns=[tcb._pattern_regex(p) for p in patterns]
	)


# two projects sharing one account and one numbering pattern, and two on their own
SHARED_A = _project("LA-SHARED-A", "191207000002", "99910#GVA####")
SHARED_B = _project("LA-SHARED-B", "191207000002", "99910#GVA####")
IHM = _project("LA-IHM", "223207000023", "99910#IHM####")
RAM = _project("LA-RAM", "191207000010", "99910#RAM####")
ALL = [SHARED_A, SHARED_B, IHM, RAM]


def _no_records(*args, **kwargs):
	return None


class TestAttribution(FrappeTestCase):
	def _attribute(self, reference, account, account_projects, lookups=_no_records):
		with patch("landms.tcb.frappe.db.get_value", side_effect=lookups):
			return tcb._attribute_recon_row({"reference": reference}, account, account_projects, ALL)

	def test_tcb_none_means_no_control_number(self):
		for value in ("None", "none", "null", "", None):
			self.assertEqual(tcb._row_reference({"reference": value}), "")
		self.assertEqual(tcb._row_reference({"reference": " 999103IHM8287 "}), "999103IHM8287")

	def test_hash_is_one_digit(self):
		rx = tcb._pattern_regex("99910#GVA####")
		self.assertTrue(rx.match("999103GVA1234"))
		self.assertFalse(rx.match("999103GVA123"))
		self.assertFalse(rx.match("999103IHM1234"))

	def test_the_order_carrying_the_number_decides(self):
		def lookups(doctype, filters, field, *args, **kwargs):
			if doctype == "Sales Order" and isinstance(filters, dict):
				return "LA-SHARED-B"
			return None

		# the pattern alone would be ambiguous between the two shared projects
		self.assertEqual(
			self._attribute("999103GVA1234", "191207000002", ["LA-SHARED-A"], lookups), "LA-SHARED-B"
		)

	def test_the_registry_decides_when_no_order_carries_it(self):
		def lookups(doctype, filters, field, *args, **kwargs):
			if doctype == "TCB Control Number":
				return "SO-REGISTRY"
			if doctype == "Sales Order" and filters == "SO-REGISTRY":
				return "LA-IHM"
			return None

		self.assertEqual(self._attribute("999103IHM0001", "223207000023", ["LA-IHM"], lookups), "LA-IHM")

	def test_an_unknown_number_fitting_one_project_goes_to_it(self):
		self.assertEqual(self._attribute("999103IHM0001", "223207000023", ["LA-IHM"]), "LA-IHM")

	def test_an_unknown_number_two_projects_could_own_is_unmatched(self):
		self.assertIsNone(self._attribute("999103GVA0001", "191207000002", ["LA-SHARED-A", "LA-SHARED-B"]))

	def test_a_credit_without_a_number_goes_to_the_accounts_only_project(self):
		self.assertEqual(self._attribute("None", "223207000023", ["LA-IHM"]), "LA-IHM")

	def test_a_credit_without_a_number_on_a_shared_account_is_unmatched(self):
		self.assertIsNone(self._attribute("", "191207000002", ["LA-SHARED-A", "LA-SHARED-B"]))


SETTINGS = {
	"enabled": 1,
	"reconciliation_enabled": 1,
	"auto_apply_reconciliation_payments": 0,  # record what is found, book nothing
	"reconciliation_lookback_days": 1,
}


class TestOneSilentAccountDoesNotStopTheOthers(FrappeTestCase):
	def setUp(self):
		self.updates = {}
		self.created = []
		self.answers = {}

		def fetch(settings, land_acquisition, start_date, end_date, reconciliation_log=None):
			return self.answers[land_acquisition["reconciliation_id"]]

		def create_log(**kwargs):
			name = "LOG-" + (kwargs.get("land_acquisition") or "COMBINED")
			self.created.append(name)
			return name

		def update_log(log_name, **kwargs):
			self.updates[log_name] = kwargs

		for target, kwargs in (
			("landms.tcb._get_tcb_settings", {"return_value": SETTINGS}),
			("landms.tcb._fetch_reconciliation_rows", {"side_effect": fetch}),
			("landms.tcb._create_recon_log", {"side_effect": create_log}),
			("landms.tcb._update_recon_log", {"side_effect": update_log}),
			("landms.tcb._append_recon_log_row", {}),
			("landms.tcb.create_tcb_api_log", {}),
			("landms.tcb._stop_reconciliation_if_requested", {"return_value": None}),
			("landms.tcb._masked_reconciliation_endpoint", {"return_value": "tcb"}),
			("landms.tcb.frappe.db.get_value", {"return_value": None}),
		):
			p = patch(target, **kwargs)
			p.start()
			self.addCleanup(p.stop)

	def _projects(self, projects):
		p = patch("landms.tcb._recon_projects", return_value=projects)
		p.start()
		self.addCleanup(p.stop)

	def test_the_answering_project_is_reconciled_and_the_silent_one_is_marked(self):
		self._projects([IHM, RAM])
		self.answers = {
			"223207000023": {"ok": False, "message": "Read timed out"},
			"191207000010": {"ok": True, "rows": [{"reference": "999103RAM0001", "amount": 1000, "ptid": 1}]},
		}
		result = tcb.run_tcb_reconciliation_job(start_date="2026-10-14", end_date="2026-10-15")
		self.assertEqual(result["status"], "Partial")
		self.assertEqual(result["ignored"], 1)  # the RAM payment was found (auto-apply off)
		self.assertIn("TCB did not answer for account 223207000023", result["message"])
		silent_log = self.updates["LOG-LA-IHM"]
		self.assertEqual(silent_log["status"], "Failed")
		self.assertIn("not reconciled", silent_log["message"])
		self.assertEqual(self.updates["LOG-LA-RAM"]["status"], "Success")

	def test_when_tcb_answers_for_no_account_the_run_fails(self):
		self._projects([IHM, RAM])
		self.answers = {
			"223207000023": {"ok": False, "message": "Read timed out"},
			"191207000010": {"ok": False, "message": "Read timed out"},
		}
		result = tcb.run_tcb_reconciliation_job(start_date="2026-10-14", end_date="2026-10-15")
		self.assertFalse(result["ok"])
		self.assertIn("TCB did not answer", result["message"])

	def test_a_combined_log_says_which_account_was_not_checked(self):
		self._projects([IHM, RAM])
		self.answers = {
			"223207000023": {"ok": True, "rows": [{"reference": "999103IHM0001", "amount": 500, "ptid": 2}]},
			"191207000010": {"ok": False, "message": "Read timed out"},
		}
		result = tcb.run_tcb_reconciliation_job(
			start_date="2026-10-14", end_date="2026-10-15", log_name="LOG-BY-HAND"
		)
		self.assertEqual(result["status"], "Partial")
		combined = self.updates["LOG-BY-HAND"]
		self.assertEqual(combined["status"], "Partial")
		self.assertEqual(combined["ignored"], 1)  # the counts survive the note
		self.assertIn("191207000010", combined["message"])
		self.assertTrue(combined["message"].startswith("all projects:"))  # not "not matched"

	def test_a_project_run_asks_only_its_own_account(self):
		self._projects([IHM])
		asked = []
		self.answers = {"223207000023": {"ok": True, "rows": []}}
		real = self.answers

		def fetch(settings, land_acquisition, start_date, end_date, reconciliation_log=None):
			asked.append(land_acquisition["reconciliation_id"])
			return real[land_acquisition["reconciliation_id"]]

		with patch("landms.tcb._fetch_reconciliation_rows", side_effect=fetch):
			tcb.run_tcb_reconciliation_job(
				start_date="2026-10-14", end_date="2026-10-15", land_acquisition="LA-IHM"
			)
		self.assertEqual(asked, ["223207000023"])

	def test_each_shared_account_is_asked_once(self):
		self._projects([SHARED_A, SHARED_B])
		asked = []

		def fetch(settings, land_acquisition, start_date, end_date, reconciliation_log=None):
			asked.append(land_acquisition["reconciliation_id"])
			return {"ok": True, "rows": []}

		with patch("landms.tcb._fetch_reconciliation_rows", side_effect=fetch):
			tcb.run_tcb_reconciliation_job(start_date="2026-10-14", end_date="2026-10-15")
		self.assertEqual(asked, ["191207000002"])
