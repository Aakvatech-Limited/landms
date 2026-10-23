"""Keep a plot order, its invoice lines and its contract in step after money
is MOVED, not paid.

A normal payment goes through Payment Entry on_submit, which refreshes the
order's paid figure, the instalment lines on the order and the invoice, and
the contract (submit at the advance, complete at full payment). Moving money
with ERPNext's own tools does none of that: Unreconcile Payment detaches it
with a direct ledger update and Payment Reconciliation re-attaches it through
a Payment Ledger Entry, so neither fires a Payment Entry event. Until now every
move had to be followed by a hand-run catch-up (every transfer since Aug 2026;
test orders 05623/05624 on 21 Oct 2026).

This hooks the same two events the Malipopay echo already uses and queues the
system's own refresh for each plot order involved, after the move has
committed (the invoice outstanding is final only then). Non-plot documents
exit at the first lookup; nothing here can raise into the posting; no bank or
Malipopay call is made.
"""

import frappe
from frappe.utils import flt

from landms.payment_sync import (
	_get_plot_invoice_name_from_sales_order,
	_sync_contract_after_payment,
	_sync_payment_schedule_rows_from_invoice,
)

JOB_PREFIX = "realloc_sync_"


def on_payment_ledger_entry_submit(doc, method=None):
	"""Payment Ledger Entry on_submit: a Payment Entry re-linked to a plot
	invoice or order by Payment Reconciliation."""
	try:
		# ERPNext rebuilds a payment's ledger rows with adv_adj=1 only when a
		# reconciliation re-links it (accounts/utils.py reconcile_against_document);
		# a plain Payment Entry submit writes them with adv_adj=0 and is already
		# followed by payment_sync.on_submit_payment_entry.
		if not doc.flags.get("adv_adj"):
			return
		if doc.get("voucher_type") != "Payment Entry" or doc.get("against_voucher_type") not in (
			"Sales Invoice",
			"Sales Order",
		):
			return
		queue_resync(
			plot_sales_orders_for(doc.against_voucher_type, doc.against_voucher_no),
			f"Payment Ledger Entry {doc.name} ({doc.voucher_no})",
		)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Reallocation sync trigger failed (posting unaffected)")


def on_unreconcile_payment_submit(doc, method=None):
	"""Unreconcile Payment on_submit: money detached from a plot invoice."""
	try:
		orders = set()
		for row in doc.get("allocations") or []:
			orders |= plot_sales_orders_for(row.reference_doctype, row.reference_name)
		queue_resync(orders, f"Unreconcile Payment {doc.name}")
	except Exception:
		frappe.log_error(frappe.get_traceback(), "Reallocation sync trigger failed (unreconcile unaffected)")


def plot_sales_orders_for(doctype, name):
	"""The submitted plot Sales Orders behind a reference, or an empty set."""
	if not doctype or not name:
		return set()
	if doctype == "Sales Order":
		if frappe.db.get_value("Sales Order", name, "plot"):
			return {name}
		return set()
	if doctype == "Sales Invoice":
		if not frappe.db.get_value("Sales Invoice", name, "is_plot_sale_invoice"):
			return set()
		linked = frappe.db.get_value("Sales Order", {"plot_sales_invoice": name, "docstatus": 1}, "name")
		if linked:
			return {linked}
		return {
			so
			for so in frappe.get_all("Sales Invoice Item", filters={"parent": name}, pluck="sales_order")
			if so and frappe.db.get_value("Sales Order", so, "plot")
		}
	return set()


def queue_resync(sales_orders, trigger):
	for so in sorted(sales_orders):
		if frappe.db.get_value("Sales Order", so, "docstatus") != 1:
			continue
		frappe.enqueue(
			"landms.reallocation_sync.resync_sales_order",
			queue="short",
			job_id=JOB_PREFIX + so,
			deduplicate=True,
			enqueue_after_commit=True,
			sales_order=so,
			trigger=trigger,
		)


def resync_sales_order(sales_order, trigger=None):
	"""The refresh a Payment Entry submit does, without a Payment Entry:
	order paid figure, instalment lines on order and invoice, invoice status,
	contract. Idempotent; safe to run on an order that is already in step."""
	try:
		if frappe.db.get_value("Sales Order", sales_order, "docstatus") != 1:
			return
		invoice_name = _get_plot_invoice_name_from_sales_order(sales_order)
		if not invoice_name:
			return
		invoice = frappe.get_doc("Sales Invoice", invoice_name)
		if invoice.docstatus != 1 or not invoice.get("is_plot_sale_invoice"):
			return

		so = frappe.get_doc("Sales Order", sales_order)
		total_paid = max(0.0, flt(invoice.grand_total) - flt(invoice.outstanding_amount))
		frappe.db.set_value(
			"Sales Order",
			so.name,
			{
				"advance_paid": flt(total_paid, so.precision("advance_paid")),
				"plot_outstanding_amount": flt(invoice.outstanding_amount),
			},
			update_modified=True,
		)
		_sync_payment_schedule_rows_from_invoice(so, invoice)
		_sync_payment_schedule_rows_from_invoice(invoice, invoice, booking_fee_percent=so.booking_fee_percent)
		# Overdue/Partly Paid/Paid is read off the instalment lines just rewritten
		frappe.get_doc("Sales Invoice", invoice_name).set_status(update=True, update_modified=False)

		if so.plot_contract and frappe.db.exists("Plot Contract", so.plot_contract):
			_sync_contract_after_payment(so.plot_contract)
		frappe.clear_document_cache("Sales Order", so.name)
	except Exception:
		frappe.log_error(frappe.get_traceback(), f"Reallocation sync failed for {sales_order} ({trigger})")
