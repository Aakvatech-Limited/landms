"""The Customer History tab on a plot (PM-7).

The one-or-two-word outcome and the money-move journal rule are tested directly; the door is
tested for who may call it and how; the tab is checked on the site's own plots: newest first,
at most one current holder, and a current holder exactly when the plot is held.

Safety: every write is rolled back; a database commit or an outside call fails the test.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from landms.landms.doctype.plot_master import customer_history
from landms.landms.doctype.plot_master.customer_history import (
	_is_money_move,
	_outcome,
	_readable,
	get_customer_history,
)

HELD = ("Pending Fee", "Pending Advance", "Reserved", "Ready for Handover", "Delivered")


def _no_commit():
	raise AssertionError("a database commit happened inside the customer history test")


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the customer history test")


def _line(**values):
	line = {"handed_over": False, "contracts": [], "orders": [], "app": None}
	line.update(values)
	return line


class TestCustomerHistory(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)
		self.addCleanup(frappe.set_user, frappe.session.user)

	def _user(self, roles):
		return (
			frappe.get_doc(
				{
					"doctype": "User",
					"email": f"pm7-{frappe.generate_hash(length=8)}@test.invalid",
					"first_name": "PM-7 test",
					"send_welcome_email": 0,
					"roles": [{"role": role} for role in roles],
				}
			)
			.insert(ignore_permissions=True)
			.name
		)

	def test_what_happened_in_one_or_two_words(self):
		d = frappe._dict
		cases = [
			(_line(handed_over=True), "Handed over"),
			(_line(contracts=[d(docstatus=1, contract_status="Completed")]), "Paid off"),
			(_line(contracts=[d(docstatus=1, contract_status="Ongoing")]), "Paying"),
			(_line(contracts=[d(docstatus=1, contract_status="Overdue")]), "Overdue"),
			(_line(contracts=[d(docstatus=1, contract_status="Terminated")]), "Terminated"),
			(_line(orders=[d(docstatus=1, status="Closed")]), "Forfeited"),
			(_line(orders=[d(docstatus=1, status="To Deliver and Bill")]), "Paying"),
			(_line(app=d(docstatus=1, status="Paid")), "Applied"),
			(_line(app=d(docstatus=2, status="Expired")), "Expired"),
			(
				_line(app=d(docstatus=2, status="Paid")),
				"Cancelled",
			),  # cancelled; old status field left behind
			(_line(orders=[d(docstatus=2, status="Cancelled")]), "Cancelled"),
		]
		for line, expected in cases:
			with self.subTest(expected=expected):
				self.assertEqual(_outcome(line), expected)

	def test_only_a_journal_between_customer_balances_counts_as_a_money_move(self):
		company = frappe.db.get_value("Company", {}, "name")
		receivable = frappe.db.get_value(
			"Account",
			{"company": company, "account_type": "Receivable", "is_group": 0, "disabled": 0},
			"name",
		)
		income = frappe.db.get_value(
			"Account", {"company": company, "root_type": "Income", "is_group": 0, "disabled": 0}, "name"
		)
		customers = frappe.get_all("Customer", filters={"disabled": 0}, pluck="name", limit=2)
		cost_center = frappe.get_cached_value("Company", company, "cost_center") or frappe.db.get_value(
			"Cost Center", {"company": company, "is_group": 0}, "name"
		)
		if not (receivable and income and len(customers) == 2 and cost_center):
			self.skipTest("this site lacks a receivable account, an income account or two customers")

		def journal(lines):
			return (
				frappe.get_doc(
					{
						"doctype": "Journal Entry",
						"company": company,
						"posting_date": today(),
						"accounts": [{"cost_center": cost_center, **line} for line in lines],
					}
				)
				.insert()
				.name
			)

		move = journal(
			[
				{
					"account": receivable,
					"party_type": "Customer",
					"party": customers[0],
					"debit_in_account_currency": 100,
				},
				{
					"account": receivable,
					"party_type": "Customer",
					"party": customers[1],
					"credit_in_account_currency": 100,
				},
			]
		)
		other = journal(
			[
				{
					"account": receivable,
					"party_type": "Customer",
					"party": customers[0],
					"credit_in_account_currency": 100,
				},
				{"account": income, "debit_in_account_currency": 100},
			]
		)
		self.assertTrue(_is_money_move(move))
		self.assertFalse(_is_money_move(other))

	def test_refuses_people_who_cannot_open_the_plot(self):
		plot = frappe.db.get_value("Plot Master", {}, "name")
		if not plot:
			self.skipTest("this site has no plot")
		frappe.set_user(self._user([]))
		with self.assertRaises(frappe.PermissionError):
			get_customer_history(plot)

	def test_a_record_type_the_viewer_may_not_read_is_left_out_not_an_error(self):
		frappe.set_user(self._user([]))
		self.assertEqual(_readable("Sales Order", {"name": "x"}, ["name"]), [])

	def test_the_door_is_post_only(self):
		self.assertIn(customer_history.get_customer_history, frappe.whitelisted)
		self.assertEqual(
			frappe.allowed_http_methods_for_whitelisted_func[customer_history.get_customer_history], ["POST"]
		)

	def test_on_this_sites_plots_newest_first_and_current_only_when_held(self):
		plots = frappe.get_all(
			"Plot Master", filters={"docstatus": 1}, fields=["name", "status"], order_by="name", limit=200
		)
		if not plots:
			self.skipTest("this site has no submitted plot")
		for plot in plots:
			lines = get_customer_history(plot.name)
			with self.subTest(plot=plot.name):
				starts = [str(line["from"] or "") for line in lines]
				self.assertEqual(starts, sorted(starts, reverse=True))
				current = [line for line in lines if line["current"]]
				self.assertLessEqual(len(current), 1)
				self.assertEqual(bool(current), plot.status in HELD)
