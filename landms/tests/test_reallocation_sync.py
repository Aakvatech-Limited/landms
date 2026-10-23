"""Moving a payment between two plot orders refreshes both orders, their
invoice lines and their contracts — the same refresh a Payment Entry submit
does, now also after Unreconcile Payment and Payment Reconciliation.

Uses two submitted plot orders of one customer that already exist on the site
(skips if there are none). Every write rolls back.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import flt

from landms import reallocation_sync


def _invoice_paid(name):
	grand, outstanding = frappe.db.get_value("Sales Invoice", name, ["grand_total", "outstanding_amount"])
	return flt(grand) - flt(outstanding)


class TestReallocationSync(FrappeTestCase):
	def setUp(self):
		p = patch.object(frappe.db, "commit", lambda *a, **k: None)
		p.start()
		self.addCleanup(p.stop)
		self.addCleanup(frappe.db.rollback)
		rows = frappe.db.sql(
			"""select so.name, so.customer, so.plot_sales_invoice, so.plot_contract, si.debit_to
			from `tabSales Order` so join `tabSales Invoice` si on si.name = so.plot_sales_invoice
			where so.docstatus = 1 and si.docstatus = 1 and ifnull(so.plot, '') != '' and so.status not in ('Closed', 'Completed')
			  and ifnull(so.plot_contract, '') != '' and si.outstanding_amount > 0
			order by so.customer, so.creation""",
			as_dict=True,
		)
		by_customer = {}
		for r in rows:
			by_customer.setdefault(r.customer, []).append(r)
		pair = None
		for lst in by_customer.values():
			for a in lst:
				pe = frappe.db.sql(
					"""select per.parent, per.allocated_amount from `tabPayment Entry Reference` per
					join `tabPayment Entry` pe on pe.name = per.parent
					where per.reference_name = %s and per.docstatus = 1 and pe.docstatus = 1 and pe.unallocated_amount = 0
					and (select count(*) from `tabPayment Entry Reference` x where x.parent = per.parent) = 1 limit 1""",
					(a.plot_sales_invoice,),
					as_dict=True,
				)
				others = [b for b in lst if b.name != a.name and b.debit_to == a.debit_to]
				if (
					pe
					and others
					and flt(
						frappe.db.get_value(
							"Sales Invoice", others[0].plot_sales_invoice, "outstanding_amount"
						)
					)
					>= flt(pe[0].allocated_amount)
				):
					pair = (a, others[0], pe[0].parent, flt(pe[0].allocated_amount))
					break
			if pair:
				break
		if not pair:
			self.skipTest(
				"no customer with two open plot orders and a whole single-reference payment to move"
			)
		self.a, self.b, self.pe, self.amount = pair

	def test_hooks_find_only_plot_orders(self):
		self.assertEqual(reallocation_sync.plot_sales_orders_for("Sales Order", self.a.name), {self.a.name})
		self.assertEqual(
			reallocation_sync.plot_sales_orders_for("Sales Invoice", self.a.plot_sales_invoice), {self.a.name}
		)
		self.assertEqual(reallocation_sync.plot_sales_orders_for("Journal Entry", "x"), set())
		self.assertEqual(reallocation_sync.plot_sales_orders_for("Sales Order", "no-such-order"), set())

	def test_move_refreshes_both_orders_and_contracts(self):
		a, b, pe, amount = self.a, self.b, self.pe, self.amount
		paid_a_before = _invoice_paid(a.plot_sales_invoice)
		paid_b_before = _invoice_paid(b.plot_sales_invoice)

		with patch("frappe.enqueue") as enq:
			u = frappe.new_doc("Unreconcile Payment")
			u.company = frappe.db.get_value("Sales Order", a.name, "company")
			u.voucher_type = "Payment Entry"
			u.voucher_no = pe
			u.add_references()
			u.insert(ignore_permissions=True)
			u.submit()
		jobs = [c.kwargs.get("sales_order") for c in enq.call_args_list if c.kwargs.get("sales_order")]
		self.assertIn(a.name, jobs, "unreconcile must queue a refresh for the old order")
		reallocation_sync.resync_sales_order(a.name, "test")
		self.assertAlmostEqual(_invoice_paid(a.plot_sales_invoice), paid_a_before - amount, places=2)
		self.assertAlmostEqual(
			flt(frappe.db.get_value("Sales Order", a.name, "advance_paid")), paid_a_before - amount, places=2
		)
		self.assertAlmostEqual(
			flt(frappe.db.get_value("Plot Contract", a.plot_contract, "total_paid")),
			paid_a_before - amount,
			places=2,
		)

		with patch("frappe.enqueue") as enq:
			pr = frappe.new_doc("Payment Reconciliation")
			pr.company = u.company
			pr.party_type = "Customer"
			pr.party = a.customer
			pr.receivable_payable_account = a.debit_to
			pr.invoice_limit = 100
			pr.payment_limit = 100
			pr.get_unreconciled_entries()
			pays = [dict(x.as_dict()) for x in pr.payments if x.reference_name == pe]
			invs = [dict(x.as_dict()) for x in pr.invoices if x.invoice_number == b.plot_sales_invoice]
			self.assertEqual((len(pays), len(invs)), (1, 1))
			pr.allocate_entries({"payments": pays, "invoices": invs})
			pr.reconcile()
		jobs = [c.kwargs.get("sales_order") for c in enq.call_args_list if c.kwargs.get("sales_order")]
		self.assertIn(b.name, jobs, "reconciliation must queue a refresh for the new order")
		reallocation_sync.resync_sales_order(b.name, "test")
		self.assertAlmostEqual(_invoice_paid(b.plot_sales_invoice), paid_b_before + amount, places=2)
		self.assertAlmostEqual(
			flt(frappe.db.get_value("Sales Order", b.name, "advance_paid")), paid_b_before + amount, places=2
		)
		self.assertAlmostEqual(
			flt(frappe.db.get_value("Plot Contract", b.plot_contract, "total_paid")),
			paid_b_before + amount,
			places=2,
		)
		lines = frappe.get_all(
			"Payment Schedule",
			filters={"parent": b.plot_sales_invoice},
			fields=["payment_amount", "outstanding"],
		)
		self.assertAlmostEqual(
			sum(flt(r.outstanding) for r in lines),
			flt(frappe.db.get_value("Sales Invoice", b.plot_sales_invoice, "outstanding_amount")),
			places=2,
		)

	def test_plain_payment_submit_is_left_to_payment_sync(self):
		"""Ledger rows a Payment Entry writes on its own submit carry no adv_adj flag."""
		doc = frappe._dict(
			voucher_type="Payment Entry",
			against_voucher_type="Sales Invoice",
			against_voucher_no=self.a.plot_sales_invoice,
			name="ple",
			voucher_no=self.pe,
			flags=frappe._dict(),
		)
		with patch("frappe.enqueue") as enq:
			reallocation_sync.on_payment_ledger_entry_submit(doc)
		enq.assert_not_called()
		doc.flags.adv_adj = 1
		with patch("frappe.enqueue") as enq:
			reallocation_sync.on_payment_ledger_entry_submit(doc)
		self.assertEqual(enq.call_args.kwargs.get("sales_order"), self.a.name)

	def test_non_plot_and_missing_documents_are_ignored_quietly(self):
		reallocation_sync.resync_sales_order("no-such-order", "test")  # must not raise
		doc = frappe._dict(
			voucher_type="Journal Entry",
			against_voucher_type="Sales Invoice",
			against_voucher_no="x",
			name="ple",
			voucher_no="je",
		)
		with patch("frappe.enqueue") as enq:
			reallocation_sync.on_payment_ledger_entry_submit(doc)
		enq.assert_not_called()
