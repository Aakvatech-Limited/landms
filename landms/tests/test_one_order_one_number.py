"""One order, one payment number — however many times Submit is pressed.

When TCB is down the submit is refused, but the number it minted stays saved
(the TCB API log commits mid-transaction) while the form never receives it.
The next Submit used to mint a second number; a customer then paid 10.26M on
the first one and every payment was refused (SO-05458, Oct 2026; reproduced on
the laptop 14 Oct: three tries, three numbers). Every write rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import sales_order_hooks
from landms.tcb import create_or_get_registry
from landms.tcb import generate_control_number as mint


class TestOneOrderOneNumber(FrappeTestCase):
	def setUp(self):
		p = patch.object(frappe.db, "commit", lambda *a, **k: None)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)
		row = frappe.db.sql("""select so.name from `tabSales Order` so
			join `tabLand Acquisition` la on la.name = so.land_acquisition
			where so.docstatus = 0 and ifnull(so.plot, '') != ''
			and ifnull(la.control_number_pattern, '') != '' limit 1""")
		if not row:
			self.skipTest("this site has no draft plot Sales Order on a project with a number pattern")
		self.so = row[0][0]

	def _form(self):
		"""The order as the form that pressed Submit sends it: no number."""
		doc = frappe.get_doc("Sales Order", self.so)
		doc.control_number = ""
		return doc

	def _hold(self, status="Generated"):
		"""Give the order a number the way a refused submit leaves it."""
		number = mint(self.so)
		create_or_get_registry(control_number=number, sales_order=self.so)
		if status != "Generated":
			frappe.db.set_value("TCB Control Number", number, "status", status)
		return number

	def test_second_submit_reuses_the_number_saved_on_the_order(self):
		number = self._hold()
		frappe.db.set_value("Sales Order", self.so, "control_number", number)
		with patch("landms.sales_order_hooks.generate_control_number") as new_number:
			self.assertEqual(sales_order_hooks._ensure_control_number(self._form()), number)
		new_number.assert_not_called()

	def test_number_blanked_by_a_save_is_found_again_in_the_registry(self):
		number = self._hold()
		frappe.db.set_value("Sales Order", self.so, "control_number", "")
		with patch("landms.sales_order_hooks.generate_control_number") as new_number:
			self.assertEqual(sales_order_hooks._ensure_control_number(self._form()), number)
		new_number.assert_not_called()
		self.assertEqual(frappe.db.get_value("Sales Order", self.so, "control_number"), number)

	def _decline_every_number_of_the_order(self):
		for name in frappe.get_all("TCB Control Number", filters={"sales_order": self.so}, pluck="name"):
			frappe.db.set_value("TCB Control Number", name, "status", "Declined")

	def test_a_declined_number_is_never_reused(self):
		self._decline_every_number_of_the_order()
		self._hold(status="Declined")
		frappe.db.set_value("Sales Order", self.so, "control_number", "")
		with patch("landms.sales_order_hooks.generate_control_number", return_value="FRESH") as new_number:
			self.assertEqual(sales_order_hooks._ensure_control_number(self._form()), "FRESH")
		new_number.assert_called_once()

	def test_an_order_without_a_number_still_gets_one(self):
		frappe.db.set_value("Sales Order", self.so, "control_number", "")
		self._decline_every_number_of_the_order()
		with patch("landms.sales_order_hooks.generate_control_number", return_value="FRESH") as new_number:
			self.assertEqual(sales_order_hooks._ensure_control_number(self._form()), "FRESH")
		new_number.assert_called_once()

	def test_retry_refuses_a_number_the_order_no_longer_uses(self):
		old = self._hold(status="Failed")
		frappe.db.set_value("Sales Order", self.so, "control_number", self._hold())
		registry = frappe.get_doc("TCB Control Number", old)
		with (
			patch("landms.tcb.register_reference_for_sales_order") as register,
			self.assertRaisesRegex(frappe.ValidationError, "no longer the payment number"),
		):
			registry.retry_registration()
		register.assert_not_called()

	def test_a_dead_number_on_the_form_is_replaced(self):
		# an amended or copied order can carry its original's switched-off number
		self._decline_every_number_of_the_order()
		dead = self._hold(status="Declined")
		doc = self._form()
		doc.control_number = dead
		with patch("landms.sales_order_hooks.generate_control_number", return_value="FRESH") as new_number:
			self.assertEqual(sales_order_hooks._ensure_control_number(doc), "FRESH")
		new_number.assert_called_once()

	def test_a_dead_stored_number_is_replaced(self):
		self._decline_every_number_of_the_order()
		frappe.db.set_value("Sales Order", self.so, "control_number", self._hold(status="Paid"))
		with patch("landms.sales_order_hooks.generate_control_number", return_value="FRESH") as new_number:
			self.assertEqual(sales_order_hooks._ensure_control_number(self._form()), "FRESH")
		new_number.assert_called_once()

	def test_another_orders_number_is_never_taken(self):
		self._decline_every_number_of_the_order()
		other = frappe.db.get_value("Sales Order", {"name": ["!=", self.so], "docstatus": ["<", 2]}, "name")
		theirs = mint(self.so)
		create_or_get_registry(control_number=theirs, sales_order=other or "SO-SOMEONE-ELSE")
		doc = self._form()
		doc.control_number = theirs
		with patch("landms.sales_order_hooks.generate_control_number", return_value="FRESH"):
			self.assertEqual(sales_order_hooks._ensure_control_number(doc), "FRESH")

	def test_retry_allowed_for_the_orders_newest_number_after_a_save_blanked_it(self):
		self._decline_every_number_of_the_order()
		number = self._hold(status="Failed")
		frappe.db.set_value("Sales Order", self.so, "control_number", "")
		registry = frappe.get_doc("TCB Control Number", number)
		with patch(
			"landms.tcb.register_reference_for_sales_order",
			return_value={"ok": True, "status": "Success", "message": "test"},
		) as register:
			registry.retry_registration()
		register.assert_called_once()

	# Scan, 14 Oct 2026: a stop that waits for TCB leaves the number at Failed on a
	# cancelled or closed order; Retry Registration must never make it live at TCB.
	# (A Draft order, whose submit failed, is the normal retry case: tested above.)

	def _retry_after(self, docstatus, status):
		number = self._hold("Failed")
		frappe.db.set_value(
			"Sales Order", self.so, {"control_number": number, "docstatus": docstatus, "status": status}
		)
		registry = frappe.get_doc("TCB Control Number", number)
		with patch("landms.payments.router.register_reference_for_sales_order") as register:
			with self.assertRaisesRegex(frappe.ValidationError, "is not registered at TCB"):
				registry.retry_registration()
		register.assert_not_called()

	def test_a_cancelled_order_is_refused(self):
		self._retry_after(2, "Cancelled")

	def test_a_closed_order_is_refused(self):
		self._retry_after(1, "Closed")
