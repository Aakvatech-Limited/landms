"""Idempotency checks for one-off setup/patch scripts.

Every patch below already ran once at site creation (post_model_sync) or at
`after_install`. Each one documents itself as idempotent — these tests call
it a second time against the real test site and prove that claim, rather
than just trusting the docstring.
"""

import frappe
from frappe.tests import IntegrationTestCase

from landms.install import after_install
from landms.patches import convert_backref_links_to_data, recalculate_la_costs_include_je
from landms.patches.setup_data import create_payment_terms, create_roles


class TestAfterInstallIsIdempotent(IntegrationTestCase):
	def test_running_after_install_again_does_not_raise(self):
		after_install()  # must not raise, must not duplicate records

		self.assertEqual(frappe.db.count("Workflow", {"workflow_name": "Land Acquisition Approval"}), 1)
		self.assertEqual(frappe.db.count("Accounting Dimension", {"document_type": "Land Acquisition"}), 1)


class TestCreateRolesIsIdempotent(IntegrationTestCase):
	def test_role_exists_and_rerun_does_not_duplicate(self):
		create_roles.execute()
		create_roles.execute()

		self.assertEqual(frappe.db.count("Role", {"role_name": "Land Acquisition Approver"}), 1)


class TestCreatePaymentTermsIsIdempotent(IntegrationTestCase):
	def test_payment_terms_exist_and_rerun_does_not_duplicate(self):
		create_payment_terms.execute()
		create_payment_terms.execute()

		self.assertEqual(frappe.db.count("Payment Term", {"payment_term_name": "Advance"}), 1)
		self.assertEqual(frappe.db.count("Payment Term", {"payment_term_name": "Balance"}), 1)


class TestRecalculateLaCostsIncludeJe(IntegrationTestCase):
	def test_runs_cleanly_with_no_submitted_land_acquisitions(self):
		recalculate_la_costs_include_je.execute()  # must not raise on an empty table


class TestConvertBackrefLinksToData(IntegrationTestCase):
	def test_runs_cleanly_when_nothing_matches(self):
		# Not wired into patches.txt — kept as an on-demand fixer. Still owned
		# by the app, so its SQL must keep executing cleanly on v16.
		convert_backref_links_to_data.execute()  # must not raise
