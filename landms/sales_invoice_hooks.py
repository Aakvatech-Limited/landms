"""Sales Invoice doc-event hooks for LandMS plot sale invoices."""

import frappe


def before_save_sales_invoice(doc, method=None):
	if not doc.get("is_plot_sale_invoice"):
		return

	# ERPNext re-enables deferred revenue from the item master on every validate.
	# Clear it so our mechanism (Customer Advances + revenue JE on full payment)
	# is not disrupted.
	for item in (doc.items or []):
		item.enable_deferred_revenue = 0
		item.deferred_revenue_account = ""
		item.service_start_date = None
		item.service_end_date = None

	# Restore the Balance row if ERPNext's validate collapsed the schedule to
	# a single Advance row. For plot SIs the SO links forward (SO.plot_sales_invoice),
	# so look it up by name if sales_order_reference is not set.
	so_ref = doc.get("sales_order_reference") or (
		frappe.db.get_value("Sales Order", {"plot_sales_invoice": doc.name, "docstatus": 1}, "name")
		if doc.name else None
	)
	if so_ref and len(doc.get("payment_schedule") or []) == 1:
		so_schedule = frappe.get_all(
			"Payment Schedule",
			filters={"parent": so_ref, "parenttype": "Sales Order"},
			fields=["payment_term", "description", "due_date", "invoice_portion", "payment_amount"],
			order_by="idx asc",
		)
		if len(so_schedule) >= 2:
			row2 = so_schedule[1]
			doc.append("payment_schedule", {
				"payment_term":    row2.payment_term,
				"description":     row2.description,
				"due_date":        row2.due_date,
				"invoice_portion": row2.invoice_portion,
				"payment_amount":  row2.payment_amount,
			})
