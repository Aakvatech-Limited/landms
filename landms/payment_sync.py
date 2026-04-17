"""Payment Entry and single-invoice sync helpers for LandMS plot sales.

The first advance may arrive against the Sales Order before the plot SI exists.
When that happens we:
  1. accept the Payment Entry against the SO,
  2. create the single plot SI on submit,
  3. let ERPNext allocate the SO advance onto that SI,
  4. mirror invoice / payment state back to the SO and Plot Contract.

After the SI exists, all later payments are normalized directly onto it.
"""

from __future__ import annotations

import frappe
from frappe.utils import cstr, flt, today

from landms.sales_order_hooks import ensure_plot_sales_invoice_for_sales_order


def validate_payment_entry(doc, method=None):
	"""Normalize plot payments onto the single plot SI once it exists."""
	if doc.docstatus != 0:
		return
	if doc.payment_type != "Receive" or doc.party_type != "Customer":
		return
	if not doc.get("references"):
		return

	normalized_rows = []
	seen_invoice_rows: dict[tuple[str, str], dict] = {}

	for row in doc.get("references") or []:
		target = _resolve_landms_plot_reference(
			row.reference_doctype,
			row.reference_name,
			create_missing_invoice=False,
		)
		if not target or not target.get("invoice_name"):
			normalized_rows.append(_reference_row_to_dict(row))
			continue

		invoice_name = target["invoice_name"]
		payment_term = cstr(getattr(row, "payment_term", "") or "").strip()
		key = (invoice_name, payment_term)
		current_amount = flt(row.allocated_amount)
		existing = seen_invoice_rows.get(key)

		if existing:
			existing["allocated_amount"] = flt(existing.get("allocated_amount")) + current_amount
			existing["outstanding_amount"] = flt(target["outstanding_amount"])
			existing["total_amount"] = flt(target["grand_total"])
			existing["due_date"] = target.get("due_date")
			existing["payment_term_outstanding"] = flt(
				getattr(row, "payment_term_outstanding", 0) or existing.get("payment_term_outstanding") or 0
			)
			existing["exchange_rate"] = flt(
				getattr(row, "exchange_rate", 0) or existing.get("exchange_rate") or 0
			) or 1
		else:
			seen_invoice_rows[key] = {
				"reference_doctype": "Sales Invoice",
				"reference_name": invoice_name,
				"allocated_amount": current_amount,
				"total_amount": flt(target["grand_total"]),
				"outstanding_amount": flt(target["outstanding_amount"]),
				"due_date": target.get("due_date"),
				"payment_term": payment_term or None,
				"payment_term_outstanding": flt(getattr(row, "payment_term_outstanding", 0) or 0),
				"exchange_rate": flt(getattr(row, "exchange_rate", 0) or 0) or 1,
			}

	doc.set("references", [])
	for row_dict in normalized_rows:
		doc.append("references", row_dict)
	for row_dict in seen_invoice_rows.values():
		doc.append("references", row_dict)


def on_submit_payment_entry(doc, method=None):
	_ensure_first_advance_plot_invoices(doc)
	_sync_landms_payment_entry_state(doc)


def on_cancel_payment_entry(doc, method=None):
	_sync_landms_payment_entry_state(doc)


def sync_plot_contract_from_payment_entry(doc, method=None):
	"""Backward-compatible hook name kept for older wiring."""
	_sync_landms_payment_entry_state(doc)


def _build_pe_references_for_invoice(si, total_allocated: float) -> list[dict]:
	"""Build PE reference rows that allocate payment across SI payment schedule terms.

	ERPNext only updates per-row paid_amount on the SI payment schedule when the
	PE reference row carries the matching payment_term. Without it, the overall
	SI outstanding updates but individual schedule rows stay at zero — breaking
	our contract sync which reads from those rows.
	"""
	schedule = si.get("payment_schedule") or []
	if not schedule:
		return [{
			"reference_doctype": "Sales Invoice",
			"reference_name": si.name,
			"allocated_amount": flt(total_allocated),
		}]

	refs = []
	remaining = flt(total_allocated)
	for row in sorted(schedule, key=lambda r: r.idx):
		if remaining <= 0:
			break
		row_outstanding = flt(row.outstanding or 0)
		if row_outstanding <= 0:
			continue
		alloc = min(remaining, row_outstanding)
		refs.append({
			"reference_doctype": "Sales Invoice",
			"reference_name": si.name,
			"allocated_amount": alloc,
			"payment_term": row.payment_term,
		})
		remaining -= alloc

	# If there's still remaining (overpayment or no matching terms), add a catch-all row
	if remaining > 0:
		refs.append({
			"reference_doctype": "Sales Invoice",
			"reference_name": si.name,
			"allocated_amount": remaining,
		})

	return refs or [{
		"reference_doctype": "Sales Invoice",
		"reference_name": si.name,
		"allocated_amount": flt(total_allocated),
	}]


def create_payment_entry_for_sales_order(
	*,
	sales_order_name: str,
	amount: float,
	payment_date: str | None,
	bank_account: str,
	reference_no: str,
	remarks: str | None = None,
	submit: bool = True,
) -> str:
	"""Create a Payment Entry against the plot SI, optionally submitting it.

	Used by TCB inbound, so if this is the first advance we create the single
	plot SI on demand before posting the PE. When `submit=False` the PE is
	inserted as a Draft for manual review instead of being auto-submitted.
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

	invoice_name = so.get("plot_sales_invoice") or ensure_plot_sales_invoice_for_sales_order(
		sales_order_name,
		posting_date=payment_date or today(),
	)
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
	pe_references = _build_pe_references_for_invoice(si, allocated)

	pe = frappe.get_doc({
		"doctype": "Payment Entry",
		"payment_type": "Receive",
		"posting_date": payment_date or today(),
		"company": si.company,
		"party_type": "Customer",
		"party": si.customer,
		"paid_from": si.debit_to,
		"paid_to": bank_account,
		"paid_amount": flt(amount),
		"received_amount": flt(amount),
		"reference_no": cstr(reference_no),
		"reference_date": payment_date or today(),
		"remarks": remarks or f"Payment against {invoice_name}",
		"references": pe_references,
	})
	pe.name = cstr(reference_no)
	pe.flags.name_set = True

	original_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		pe.insert(ignore_permissions=True, set_name=cstr(reference_no))
		if submit:
			pe.submit()
	finally:
		frappe.set_user(original_user)
	return pe.name


def _ensure_first_advance_plot_invoices(doc):
	for row in doc.get("references") or []:
		if row.reference_doctype != "Sales Order" or not row.reference_name:
			continue

		so_info = frappe.db.get_value(
			"Sales Order",
			row.reference_name,
			["name", "docstatus", "plot", "plot_sales_invoice"],
			as_dict=True,
		)
		if not so_info or so_info.docstatus != 1 or not so_info.plot or so_info.plot_sales_invoice:
			continue

		ensure_plot_sales_invoice_for_sales_order(
			so_info.name,
			posting_date=doc.posting_date or today(),
		)


def _sync_landms_payment_entry_state(doc):
	related = _get_related_landms_documents_from_payment_entry(doc)

	for sales_order_name in sorted(related["sales_orders"]):
		_sync_sales_order_from_plot_invoice(sales_order_name)

	for contract_name in sorted(related["contracts"]):
		if not frappe.db.exists("Plot Contract", contract_name):
			continue
		contract = frappe.get_doc("Plot Contract", contract_name)
		contract.sync_payment_status()


def _get_related_landms_documents_from_payment_entry(doc) -> dict[str, set[str]]:
	sales_orders = set()
	contracts = set()

	for row in doc.get("references") or []:
		target = _resolve_landms_plot_reference(
			row.reference_doctype,
			row.reference_name,
			create_missing_invoice=False,
		)
		if not target:
			continue

		if target.get("sales_order_name"):
			sales_orders.add(target["sales_order_name"])
		if target.get("plot_contract"):
			contracts.add(target["plot_contract"])

	return {
		"sales_orders": sales_orders,
		"contracts": contracts,
	}


def _resolve_landms_plot_reference(
	reference_doctype: str | None,
	reference_name: str | None,
	*,
	create_missing_invoice: bool = False,
):
	if not reference_doctype or not reference_name:
		return None

	if reference_doctype == "Sales Order":
		if not frappe.db.exists("Sales Order", reference_name):
			return None

		so = frappe.db.get_value(
			"Sales Order",
			reference_name,
			["name", "plot", "plot_sales_invoice", "plot_contract"],
			as_dict=True,
		)
		if not so or not so.plot:
			return None

		invoice_name = _get_plot_invoice_name_from_sales_order(reference_name)
		if not invoice_name and create_missing_invoice:
			invoice_name = ensure_plot_sales_invoice_for_sales_order(reference_name)
		if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
			return {
				"sales_order_name": so.name,
				"invoice_name": "",
				"plot_contract": so.plot_contract or "",
				"grand_total": 0,
				"outstanding_amount": 0,
				"due_date": None,
			}

		invoice = frappe.db.get_value(
			"Sales Invoice",
			invoice_name,
			["name", "is_plot_sale_invoice", "grand_total", "outstanding_amount", "due_date", "plot_contract"],
			as_dict=True,
		)
		if not invoice or not invoice.is_plot_sale_invoice:
			return None

		return {
			"sales_order_name": so.name,
			"invoice_name": invoice.name,
			"plot_contract": invoice.plot_contract or so.plot_contract or "",
			"grand_total": invoice.grand_total,
			"outstanding_amount": invoice.outstanding_amount,
			"due_date": invoice.due_date,
		}

	if reference_doctype == "Sales Invoice":
		if not frappe.db.exists("Sales Invoice", reference_name):
			return None

		invoice = frappe.db.get_value(
			"Sales Invoice",
			reference_name,
			["name", "is_plot_sale_invoice", "grand_total", "outstanding_amount", "due_date", "plot_contract"],
			as_dict=True,
		)
		if not invoice or not invoice.is_plot_sale_invoice:
			return None

		sales_order_name = _get_sales_order_name_from_plot_invoice(reference_name)
		return {
			"sales_order_name": sales_order_name or "",
			"invoice_name": invoice.name,
			"plot_contract": invoice.plot_contract
				or frappe.db.get_value("Sales Order", sales_order_name, "plot_contract")
				or "",
			"grand_total": invoice.grand_total,
			"outstanding_amount": invoice.outstanding_amount,
			"due_date": invoice.due_date,
		}

	return None


def _reference_row_to_dict(row):
	return {
		"reference_doctype": row.reference_doctype,
		"reference_name": row.reference_name,
		"allocated_amount": flt(row.allocated_amount),
		"total_amount": flt(getattr(row, "total_amount", 0) or 0),
		"outstanding_amount": flt(getattr(row, "outstanding_amount", 0) or 0),
		"due_date": getattr(row, "due_date", None),
		"payment_term": getattr(row, "payment_term", None),
		"payment_term_outstanding": flt(getattr(row, "payment_term_outstanding", 0) or 0),
		"exchange_rate": flt(getattr(row, "exchange_rate", 0) or 0) or 1,
	}


def _sync_sales_order_from_plot_invoice(sales_order_name: str):
	if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
		return

	so = frappe.get_doc("Sales Order", sales_order_name)
	invoice_name = _get_plot_invoice_name_from_sales_order(sales_order_name)
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		return

	invoice = frappe.get_doc("Sales Invoice", invoice_name)
	if not getattr(invoice, "is_plot_sale_invoice", 0):
		return

	total_paid = max(0.0, flt(invoice.grand_total) - flt(invoice.outstanding_amount))
	_sync_sales_order_billing_from_plot_invoice(so, invoice)
	_sync_payment_schedule_rows_from_invoice(so, invoice)

	if so.get("plot_sales_invoice") != invoice.name:
		frappe.db.set_value("Sales Order", so.name, "plot_sales_invoice", invoice.name, update_modified=False)

	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"advance_paid": flt(total_paid, so.precision("advance_paid")),
			"plot_outstanding_amount": flt(invoice.outstanding_amount),
		},
		update_modified=True,
	)
	frappe.clear_document_cache("Sales Order", so.name)


def _sync_sales_order_billing_from_plot_invoice(so, invoice):
	if not so.items:
		return

	_link_invoice_items_to_sales_order_rows(so, invoice)
	invoice.reload()
	invoice.update_status_updater_args()
	invoice.update_prevdoc_status()
	so.reload()

	total_amount = sum(abs(flt(row.amount)) for row in so.items)
	billed_amount = sum(
		min(abs(flt(row.amount)), abs(flt(frappe.db.get_value("Sales Order Item", row.name, "billed_amt") or 0)))
		for row in so.items
	)
	per_billed = round((billed_amount / total_amount) * 100, 6) if total_amount else 0.0

	if per_billed < 0.001:
		billing_status = "Not Billed"
	elif per_billed > 99.999999:
		billing_status = "Fully Billed"
	else:
		billing_status = "Partly Billed"

	frappe.db.set_value(
		"Sales Order",
		so.name,
		{
			"per_billed": per_billed,
			"billing_status": billing_status,
		},
		update_modified=True,
	)

	so.reload()
	so.set_status(update=True, update_modified=True)


def _link_invoice_items_to_sales_order_rows(so, invoice):
	so_items = so.get("items") or []
	if not so_items:
		return

	default_so_item = so_items[0].name
	so_item_by_code = {row.item_code: row.name for row in so_items if row.item_code}

	for item in invoice.get("items") or []:
		updates = {}
		if item.sales_order != so.name:
			updates["sales_order"] = so.name

		target_so_detail = item.so_detail or so_item_by_code.get(item.item_code) or default_so_item
		if target_so_detail and item.so_detail != target_so_detail:
			updates["so_detail"] = target_so_detail

		if updates:
			frappe.db.set_value("Sales Invoice Item", item.name, updates, update_modified=False)


def _sync_payment_schedule_rows_from_invoice(parent_doc, invoice):
	source_rows = invoice.get("payment_schedule") or []
	target_rows = parent_doc.get("payment_schedule") or []
	limit = min(len(source_rows), len(target_rows))

	for idx in range(limit):
		source = source_rows[idx]
		target = target_rows[idx]
		frappe.db.set_value(
			"Payment Schedule",
			target.name,
			{
				"due_date": source.due_date,
				"invoice_portion": flt(source.invoice_portion),
				"payment_amount": flt(source.payment_amount),
				"base_payment_amount": flt(source.base_payment_amount or 0),
				"paid_amount": flt(source.paid_amount or 0),
				"outstanding": flt(source.outstanding or 0),
				"base_paid_amount": flt(source.base_paid_amount or 0),
				"base_outstanding": flt(source.base_outstanding or 0),
			},
			update_modified=True,
		)


def _get_plot_invoice_name_from_sales_order(sales_order_name: str) -> str:
	invoice_name = frappe.db.get_value("Sales Order", sales_order_name, "plot_sales_invoice")
	if invoice_name and frappe.db.exists("Sales Invoice", invoice_name):
		return invoice_name

	result = frappe.db.sql(
		"""
		select si.name
		from `tabSales Invoice` si
		inner join `tabSales Invoice Item` sii on sii.parent = si.name
		where si.docstatus != 2
		  and si.is_return = 0
		  and ifnull(si.is_plot_sale_invoice, 0) = 1
		  and sii.sales_order = %s
		order by si.modified desc
		limit 1
		""",
		(sales_order_name,),
		as_dict=True,
	)
	return result[0].name if result else ""


def _get_sales_order_name_from_plot_invoice(invoice_name: str) -> str:
	sales_order_name = frappe.db.get_value(
		"Sales Order",
		{"plot_sales_invoice": invoice_name, "docstatus": 1},
		"name",
	)
	if sales_order_name:
		return sales_order_name

	result = frappe.db.sql(
		"""
		select sii.sales_order
		from `tabSales Invoice Item` sii
		where sii.parent = %s
		  and ifnull(sii.sales_order, '') != ''
		order by sii.idx asc
		limit 1
		""",
		(invoice_name,),
		as_dict=True,
	)
	return result[0].sales_order if result else ""


def _payment_reference_exists(invoice_name: str, reference_no: str) -> bool:
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
