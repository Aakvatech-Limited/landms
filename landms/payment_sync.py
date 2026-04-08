"""Payment Entry helpers shared by TCB inbound and Plot Contract sync.

Two responsibilities:

  1. create_payment_entry_for_sales_order(...)
     Programmatic Payment Entry creation against a Sales Order's plot Sales
     Invoice. Used by TCB inbound (IPN + reconciliation) to apply a confirmed
     payment without going through the UI.

  2. sync_plot_contract_from_payment_entry(doc, method)
     Hook entrypoint wired into Payment Entry submit/cancel events. Walks
     PE → SI → SO → Plot Contract and calls Plot Contract.sync_payment_status()
     so the contract totals, schedule rows, and Plot Master status stay in
     lockstep with the underlying invoice.
"""

import frappe
from frappe.utils import cstr, flt, today


# ---------------------------------------------------------------------- #
#  Programmatic Payment Entry creation                                     #
# ---------------------------------------------------------------------- #


def create_payment_entry_for_sales_order(
	*,
	sales_order_name: str,
	amount: float,
	payment_date: str | None,
	bank_account: str,
	reference_no: str,
	remarks: str | None = None,
) -> str:
	"""Create + submit a Payment Entry against the SO's plot Sales Invoice.

	Idempotency: throws if a submitted PE with the same reference_no already
	exists for this invoice. The error message starts with 'Duplicate payment
	reference' so callers can distinguish it from real failures.

	Returns the PE name on success.
	"""
	if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
		frappe.throw(f"Sales Order {sales_order_name} not found.")
	if flt(amount) <= 0:
		frappe.throw("Payment amount must be greater than zero.")
	if not bank_account:
		frappe.throw("Bank/Cash account is required to create the Payment Entry.")
	if not reference_no:
		frappe.throw("Payment reference number is required (used for idempotency).")

	so = frappe.get_doc("Sales Order", sales_order_name)
	if so.docstatus != 1:
		frappe.throw(f"Sales Order {sales_order_name} is not submitted.")

	invoice_name = so.get("plot_sales_invoice")
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		frappe.throw(
			f"Sales Order {sales_order_name} has no plot Sales Invoice yet. "
			"Cannot apply payment."
		)

	if _payment_reference_exists(invoice_name, reference_no):
		frappe.throw(
			f"Duplicate payment reference {reference_no} for Sales Invoice {invoice_name}."
		)

	si = frappe.get_doc("Sales Invoice", invoice_name)
	if si.docstatus != 1:
		frappe.throw(f"Sales Invoice {invoice_name} is not submitted.")

	outstanding = flt(si.outstanding_amount)
	if outstanding <= 0:
		frappe.throw(
			f"Sales Invoice {invoice_name} has no outstanding balance. "
			"Nothing to apply."
		)

	allocated = min(flt(amount), outstanding)
	pe = frappe.get_doc({
		"doctype":         "Payment Entry",
		"payment_type":    "Receive",
		"posting_date":    payment_date or today(),
		"company":         si.company,
		"party_type":      "Customer",
		"party":           si.customer,
		"paid_from":       si.debit_to,
		"paid_to":         bank_account,
		"paid_amount":     flt(amount),
		"received_amount": flt(amount),
		"reference_no":    cstr(reference_no),
		"reference_date":  payment_date or today(),
		"remarks":         remarks or f"Payment against {invoice_name}",
		"references": [{
			"reference_doctype": "Sales Invoice",
			"reference_name":    invoice_name,
			"allocated_amount":  allocated,
		}],
	})
	pe.insert(ignore_permissions=True)
	pe.submit()
	return pe.name


def _payment_reference_exists(invoice_name: str, reference_no: str) -> bool:
	"""True if a submitted PE with this reference_no already references the SI."""
	if not invoice_name or not reference_no:
		return False
	rows = frappe.db.sql(
		"""
		select pe.name
		from `tabPayment Entry` pe
		inner join `tabPayment Entry Reference` per
			on per.parent = pe.name
		where pe.docstatus = 1
		  and pe.reference_no = %s
		  and per.reference_doctype = 'Sales Invoice'
		  and per.reference_name = %s
		limit 1
		""",
		(reference_no, invoice_name),
	)
	return bool(rows)


# ---------------------------------------------------------------------- #
#  Hook: Payment Entry submit/cancel → Plot Contract sync                  #
# ---------------------------------------------------------------------- #


def sync_plot_contract_from_payment_entry(doc, method=None):
	"""Walk PE → SI → SO → Plot Contract and refresh contract totals.

	Wired to Payment Entry on_submit / on_cancel. Iterates over every
	Sales Invoice the PE references, finds the SO that owns the invoice,
	and if the SO has a linked Plot Contract, calls sync_payment_status()
	on it. No-op when:

	  - the PE doesn't touch any Sales Invoice
	  - the invoice isn't a plot SI (no SO links back to it)
	  - the SO has no plot_contract link
	"""
	for ref in (doc.references or []):
		if ref.reference_doctype != "Sales Invoice" or not ref.reference_name:
			continue
		_sync_invoice(ref.reference_name)


def _sync_invoice(invoice_name: str) -> None:
	"""Find the SO whose plot_sales_invoice is this invoice, and sync its contract."""
	try:
		# In landms the SO holds the plot_sales_invoice link, not the other way
		# around. Find SOs that point at this invoice.
		so_names = frappe.db.get_all(
			"Sales Order",
			filters={"plot_sales_invoice": invoice_name, "docstatus": 1},
			pluck="name",
		)
	except Exception:
		so_names = []

	for so_name in so_names:
		contract_name = frappe.db.get_value("Sales Order", so_name, "plot_contract")
		if not contract_name or not frappe.db.exists("Plot Contract", contract_name):
			continue
		contract = frappe.get_doc("Plot Contract", contract_name)
		try:
			contract.sync_payment_status()
		except Exception:
			frappe.logger("landms").error(
				f"sync_payment_status failed for {contract_name}",
				exc_info=True,
			)
