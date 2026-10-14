"""A step that borrows Administrator gives the user back their whole login.

Three LandMS steps run as Administrator for a moment: building the plot
invoice, posting the forfeiture credit note (Close with money, Terminate) and
recording a payment. Switching back with frappe.set_user() alone emptied the
browser's session, and the next click failed with "User None is disabled"
(seen on the laptop 14 Oct 2026 right after a Terminate). Every write rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from landms.elevation import become_administrator, restore_caller


class TestKeepLoginAfterAdminStep(FrappeTestCase):
	def setUp(self):
		session = frappe.local.session
		self._saved = (session.user, session.get("sid"), session.get("data"))
		self.addCleanup(self._put_back)
		p = patch.object(frappe.db, "commit", lambda *a, **k: None)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)
		# a browser user's login card, as Frappe keeps it during a request
		self.card = frappe._dict(
			user="Administrator", session_ip="10.0.0.1", session_expiry="240:00:00", csrf_token="test-token"
		)
		session.sid = "test-browser-sid"
		session.data = self.card

	def _put_back(self):
		user, sid, data = self._saved
		frappe.set_user(user)
		frappe.local.session.sid = sid
		frappe.local.session.data = data

	def _card_is_whole(self):
		session = frappe.local.session
		self.assertEqual(session.sid, "test-browser-sid")
		self.assertIs(session.data, self.card)
		self.assertEqual(session.data.session_ip, "10.0.0.1")

	def test_plain_switch_back_empties_the_card(self):
		# the bug, proven: the name comes back, the rest of the card does not
		original = frappe.session.user
		frappe.set_user("Administrator")
		frappe.set_user(original)
		self.assertEqual(frappe.session.user, original)
		self.assertNotEqual(frappe.local.session.sid, "test-browser-sid")
		self.assertNotIn("session_ip", frappe.local.session.data)

	def test_helper_gives_the_whole_card_back(self):
		caller = become_administrator()
		restore_caller(caller)
		self._card_is_whole()

	def test_inside_the_step_it_is_administrator_and_then_the_caller_again(self):
		frappe.set_user("Guest")
		frappe.local.session.sid = "test-browser-sid"
		frappe.local.session.data = self.card
		caller = become_administrator()
		self.assertEqual(frappe.session.user, "Administrator")
		restore_caller(caller)
		self.assertEqual(frappe.session.user, "Guest")
		self._card_is_whole()

	def test_card_comes_back_even_when_the_step_fails(self):
		def failing_step():
			caller = become_administrator()
			try:
				raise frappe.ValidationError("step failed")
			finally:
				restore_caller(caller)

		with self.assertRaises(frappe.ValidationError):
			failing_step()
		self._card_is_whole()

	def test_recording_a_payment_keeps_the_card(self):
		from landms.payment_sync import create_payment_entry_for_sales_order

		row = frappe.db.sql("""select so.name from `tabSales Order` so
			join `tabSales Invoice` si on si.name = so.plot_sales_invoice
			where so.docstatus = 1 and si.docstatus = 1 and si.outstanding_amount > 10 limit 1""")
		bank = frappe.db.get_single_value("LandMS Settings", "tcb_bank_account")
		if not row or not bank:
			self.skipTest("this site has no open plot invoice or no TCB bank account set")
		create_payment_entry_for_sales_order(
			sales_order_name=row[0][0],
			amount=10,
			payment_date=today(),
			bank_account=bank,
			reference_no="TEST-KEEP-LOGIN-1",
			submit=False,
		)
		self._card_is_whole()

	def test_forfeiture_credit_note_keeps_the_card(self):
		from landms.sales_order_hooks import _post_credit_note_for_outstanding

		row = frappe.db.sql("""select name from `tabSales Invoice`
			where docstatus = 1 and is_plot_sale_invoice = 1 and is_return = 0
			and outstanding_amount > 0 limit 1""")
		if not row:
			self.skipTest("this site has no open plot invoice")
		self.assertTrue(_post_credit_note_for_outstanding(row[0][0]))
		self._card_is_whole()
