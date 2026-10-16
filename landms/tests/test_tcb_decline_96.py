"""TCB's decline answer 96 "Reference no does not exist" means nothing is left
to release — the stop (cancel / close / terminate) must go ahead.

Reading it as a failure jammed orders for good when a decline had timed out
but gone through (SO-03510, SO-04965, Oct 2026). The bank is faked; every write
rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import tcb

SETTINGS = frappe._dict(
	enabled=1,
	decline_reference_on_so_cancel=1,
	outbound_mode="Live",
	verify_ssl=1,
	connect_timeout_seconds=1,
	read_timeout_seconds=1,
)


class _Reply:
	def __init__(self, status, message):
		self.status_code = 200
		self.ok = True
		self.text = ""
		self.headers = {"content-type": "application/json"}
		self._body = {"Status": status, "Message": message}

	def json(self):
		return self._body


class _Session:
	def __init__(self, reply):
		self.reply = reply

	def post(self, *args, **kwargs):
		return self.reply


class TestTcbDecline96(FrappeTestCase):
	def setUp(self):
		row = frappe.db.sql("""select so.name from `tabSales Order` so
			join `tabLand Acquisition` la on la.name = so.land_acquisition
			where ifnull(la.partner_code, '') != '' and ifnull(la.profile_id, '') != ''
			and ifnull(la.control_number_pattern, '') != '' limit 1""")
		if not row:
			self.skipTest("this site has no order on a project with TCB partner codes")
		self.so = row[0][0]
		self.reply = None
		for target, kwargs in (
			("landms.tcb._get_tcb_settings", {"return_value": SETTINGS}),
			("landms.tcb._validate_live_reference_settings", {"return_value": None}),
			("landms.tcb._reference_decline_url", {"return_value": "https://tcb.test/decline"}),
			("landms.tcb._masked_reference_decline_endpoint", {"return_value": "https://tcb.test/decline"}),
			("landms.tcb.get_request_session", {"side_effect": lambda: _Session(self.reply)}),
			("landms.tcb.create_tcb_api_log", {"return_value": None}),
		):
			p = patch(target, **kwargs)
			p.start()
			self.addCleanup(p.stop)
		p = patch.object(frappe.db, "commit", lambda *a, **k: None)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)

	def _number(self, status):
		number = tcb.generate_control_number(self.so)
		tcb.create_or_get_registry(control_number=number, sales_order=self.so)
		frappe.db.set_value("TCB Control Number", number, "status", status)
		return number

	def _decline(self, number, status, message):
		self.reply = _Reply(status, message)
		return tcb.decline_reference_for_sales_order(self.so, number)

	def test_96_on_a_registered_number_releases_it(self):
		number = self._number("Registered")
		result = self._decline(number, 96, "Reference no does not exist")
		self.assertTrue(result["ok"])
		self.assertEqual(result["status"], "Already Released")
		self.assertEqual(frappe.db.get_value("TCB Control Number", number, "status"), "Declined")

	def test_96_on_a_paid_number_lets_the_stop_go_and_keeps_the_money_record(self):
		number = self._number("Paid")
		self.assertTrue(self._decline(number, 96, "Reference no does not exist")["ok"])
		self.assertEqual(frappe.db.get_value("TCB Control Number", number, "status"), "Paid")

	def test_96_on_a_never_registered_number_lets_the_stop_go(self):
		number = self._number("Failed")
		self.assertTrue(self._decline(number, 96, "Reference no does not exist")["ok"])
		self.assertEqual(frappe.db.get_value("TCB Control Number", number, "status"), "Declined")

	def test_accepted_decline_on_a_reopened_number_no_longer_throws(self):
		number = self._number("Reopened")
		self.assertTrue(self._decline(number, 0, "Success")["ok"])

	def test_accepted_decline_still_declines(self):
		number = self._number("Registered")
		result = self._decline(number, 0, "Success")
		self.assertEqual(result["status"], "Success")
		self.assertEqual(frappe.db.get_value("TCB Control Number", number, "status"), "Declined")

	def test_96_with_other_words_is_not_trusted(self):
		# in bank switching 96 can also mean "system malfunction"
		number = self._number("Registered")
		self.assertFalse(self._decline(number, 96, "System malfunction")["ok"])
		self.assertEqual(frappe.db.get_value("TCB Control Number", number, "status"), "Registered")

	def test_a_real_refusal_still_blocks(self):
		number = self._number("Registered")
		self.assertFalse(self._decline(number, 1, "failed to validate partner")["ok"])
		self.assertEqual(frappe.db.get_value("TCB Control Number", number, "status"), "Registered")
