"""Regression test for the ERPNext 15.116 account_perm_check bug.

`make_sales_invoice()` reads the customer's receivable Account via
`get_party_account()` during `set_missing_values` — i.e. while the invoice is
being BUILT, before it is inserted. ERPNext 15.116+ permission-gates that read
(`account_perm_check`), so it FAILS for the anonymous `Guest` user that the
public TCB IPN callback runs as.

The plot Sales Invoice must therefore be BUILT while the session is already
elevated to Administrator — not merely inserted/submitted under elevation
(elevating only insert/submit is "too late": the vulnerable call is the build).

These tests assert that ordering invariant directly, so they go red on the
buggy code even on a 15.115 dev bench (which lacks `account_perm_check`), and
green once the elevation wraps the build.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import payment_sync, sales_order_hooks


class _BuildReached(Exception):
	"""Sentinel raised the instant the invoice build is entered, so we can read
	frappe.session.user without faking a whole Sales Invoice."""


class TestGuestIpnElevation(FrappeTestCase):
	def setUp(self):
		self._orig_user = frappe.session.user
		self.addCleanup(lambda: frappe.set_user(self._orig_user))

	def test_invoice_is_built_as_administrator_when_called_as_guest(self):
		"""_ensure_plot_sales_invoice must build the SI as Administrator, even
		when the caller is Guest (the live IPN path)."""
		recorded = {}

		def spy_make_sales_invoice(source_name, ignore_permissions=False, **kwargs):
			recorded["user_at_build"] = frappe.session.user
			raise _BuildReached  # short-circuit before any DB write

		# frappe._dict returns None for missing keys, so .get("plot_sales_invoice")
		# is falsy and db.sql -> [] forces the code down to the make_sales_invoice
		# build (the vulnerable call).
		doc = frappe._dict(name="SAL-ORD-GUEST-TEST", plot="PLOT-X", customer="CUST-X")

		with (
			patch("landms.sales_order_hooks.frappe.db.sql", return_value=[]),
			patch(
				"erpnext.selling.doctype.sales_order.sales_order.make_sales_invoice",
				spy_make_sales_invoice,
			),
		):
			frappe.set_user("Guest")
			with self.assertRaises(_BuildReached):
				sales_order_hooks._ensure_plot_sales_invoice(doc, contract_name="")

		# Buggy code records "Guest" (build ran unelevated) -> fails.
		# Fixed code records "Administrator" -> passes.
		self.assertEqual(recorded["user_at_build"], "Administrator")

	def test_payment_path_builds_invoice_as_administrator_when_guest(self):
		"""The payment entry path (create_payment_entry_for_sales_order) reaches
		the same build via ensure_plot_sales_invoice_for_sales_order on a new
		order's first advance — it too must run elevated."""
		recorded = {}

		def spy_ensure(sales_order_name, posting_date=None, **kwargs):
			recorded["user_at_build"] = frappe.session.user
			raise _BuildReached

		so = frappe._dict(name="SAL-ORD-GUEST-TEST", docstatus=1, plot_sales_invoice=None)

		with (
			patch("landms.payment_sync.frappe.db.exists", return_value=True),
			patch("landms.payment_sync.frappe.get_doc", return_value=so),
			patch("landms.payment_sync.ensure_plot_sales_invoice_for_sales_order", spy_ensure),
		):
			frappe.set_user("Guest")
			with self.assertRaises(_BuildReached):
				payment_sync.create_payment_entry_for_sales_order(
					"SAL-ORD-GUEST-TEST",
					amount=100000,
					bank_account="Bank - X",
					reference_no="REF-GUEST-TEST",
				)

		self.assertEqual(recorded["user_at_build"], "Administrator")
