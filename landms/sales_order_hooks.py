"""Sales Order lifecycle hooks for LandMS plot sales.

The Sales Order is the commercial anchor for the plot sale:

  Plot Application (application fee paid)
    -> Sales Order (submitted, gets TCB control number)
    -> Draft Plot Contract (created immediately from SO)
    -> First advance payment
    -> Single Plot Sales Invoice (created on first advance only)
    -> Plot Contract auto-submits and mirrors SI / PE state

Important business rules from the user / legacy LMS:
  - The application fee is separate; it never becomes part of the plot SI.
  - The plot SI is created only after the first advance is received.
  - All later installments continue against that same single SI.
  - If the first advance is never received before the application validity
    window expires, the application and its Sales Order are auto-cancelled.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, cint, cstr, flt, getdate, today

from landms.landms.doctype.plot_master.plot_master import PLOT_TYPE_TO_ITEM
from landms.tcb import (
	_get_tcb_settings,
	create_or_get_registry,
	decline_reference_for_sales_order,
	generate_control_number,
	is_valid_control_number,
	is_valid_tcb_mobile,
	register_reference_for_sales_order,
)


@frappe.whitelist()
def get_sales_order_defaults(plot_application):
	"""Return all fields needed to populate a Sales Order from a Plot Application."""
	if not plot_application or not frappe.db.exists("Plot Application", plot_application):
		frappe.throw(f"Plot Application {plot_application} not found.")

	app = frappe.get_doc("Plot Application", plot_application)
	if app.docstatus != 1 or app.status != "Paid":
		frappe.throw(f"Plot Application {app.name} must be Paid. Current status: {app.status}")

	plot = frappe.get_doc("Plot Master", app.plot)
	settings = frappe.get_single("LandMS Settings")

	payment_completion_days = cint(plot.payment_completion_days or 0)
	transaction_date = app.payment_date or today()
	payment_deadline = add_days(transaction_date, payment_completion_days)

	item_row = build_sales_order_item_row(plot, settings.plot_inventory_warehouse, payment_deadline)
	schedule_rows = build_payment_schedule_rows(
		total_amount=flt(plot.selling_price),
		booking_fee_percent=flt(plot.booking_fee_percent),
		transaction_date=transaction_date,
		payment_deadline=payment_deadline,
	)

	return {
		"customer": app.customer,
		"plot": app.plot,
		"land_acquisition": plot.land_acquisition,
		"acquisition_name": plot.acquisition_name,
		"booking_fee_percent": flt(plot.booking_fee_percent),
		"government_share_percent": flt(plot.government_share_percent),
		"payment_completion_days": payment_completion_days,
		"transaction_date": transaction_date,
		"delivery_date": payment_deadline,
		"payment_deadline": payment_deadline,
		"company": settings.company,
		"set_warehouse": settings.plot_inventory_warehouse,
		"item_row": item_row,
		"schedule_rows": schedule_rows,
	}


def validate_sales_order(doc, method=None):
	if not _is_landms_sales_order(doc):
		return

	settings = frappe.get_single("LandMS Settings")
	application = _get_application(doc, required=False)

	if application:
		doc.plot = application.plot
		doc.customer = application.customer

	plot = _get_plot(doc)
	doc.company = doc.company or settings.company
	doc.land_acquisition = plot.land_acquisition
	doc.acquisition_name = plot.acquisition_name
	doc.booking_fee_percent = flt(plot.booking_fee_percent)
	doc.government_share_percent = flt(plot.government_share_percent)
	doc.payment_completion_days = cint(plot.payment_completion_days or 0)
	doc.transaction_date = doc.transaction_date or (application.payment_date if application else today())
	doc.payment_deadline = add_days(doc.transaction_date, cint(doc.payment_completion_days or 0))
	doc.set_warehouse = doc.set_warehouse or settings.plot_inventory_warehouse
	doc.ignore_default_payment_terms_template = 1

	_block_manual_control_number(doc)

	if application:
		_validate_application_window(application)
		_ensure_single_sales_order_for_application(doc, application)

	_validate_plot_state(plot)
	_validate_customer_mobile_for_tcb(doc)
	_ensure_items(doc, plot, settings)
	_ensure_payment_schedule(doc, plot)


def submit_sales_order(doc, method=None):
	if not _is_landms_sales_order(doc):
		return

	doc.db_set("plot_outstanding_amount", flt(doc.grand_total), update_modified=False)
	doc.plot_outstanding_amount = flt(doc.grand_total)

	control_number = _ensure_control_number(doc)
	_create_registry_row(doc, control_number)
	_link_application_to_sales_order(doc)
	contract_name = _ensure_draft_plot_contract(doc)
	if contract_name and doc.get("plot_contract") != contract_name:
		doc.db_set("plot_contract", contract_name, update_modified=False)
		doc.plot_contract = contract_name
	_register_with_tcb(doc, control_number)
	_mark_plot_pending_advance(doc)


def before_cancel_sales_order(doc, method=None):
	"""Delete the draft Plot Contract before Frappe's link-check runs.

	Frappe v15 blocks cancellation if any submittable doc (even in Draft state)
	has a Link field pointing to the Sales Order. The Plot Contract is
	submittable and links here, so it must be removed in before_cancel —
	which fires before the link validation — not in on_cancel.
	"""
	if not _is_landms_sales_order(doc):
		return
	_delete_draft_plot_contract(doc)


def cancel_sales_order(doc, method=None):
	if not _is_landms_sales_order(doc):
		return

	_block_cancel_if_paid(doc)
	_clear_application_sales_order_link(doc)
	_cancel_unpaid_plot_sales_invoice(doc)
	_delete_draft_plot_contract(doc)

	control_number = cstr(doc.get("control_number") or "").strip()
	if control_number:
		result = decline_reference_for_sales_order(doc.name, control_number)
		if not result.get("ok") and result.get("block_cancel"):
			frappe.throw(
				f"TCB Decline call failed for control number {control_number} and "
				f"the Decline Failure Policy is set to 'Block Cancel'. "
				f"Cancellation aborted. Detail: {result.get('message')}"
			)

	_release_plot_if_no_active_application(doc)


def ensure_plot_sales_invoice_for_sales_order(
	sales_order_name: str,
	*,
	posting_date: str | None = None,
) -> str:
	"""Create the single plot SI on first advance, or return the existing one.

	This is called from:
	  - TCB inbound payment application
	  - manual Payment Entry submit against the Sales Order
	"""
	if not sales_order_name or not frappe.db.exists("Sales Order", sales_order_name):
		frappe.throw(f"Sales Order {sales_order_name} was not found.")

	doc = frappe.get_doc("Sales Order", sales_order_name)
	contract_name = _ensure_draft_plot_contract(doc)
	invoice_name = _ensure_plot_sales_invoice(doc, contract_name, posting_date=posting_date)

	if invoice_name and doc.get("plot_sales_invoice") != invoice_name:
		doc.db_set("plot_sales_invoice", invoice_name, update_modified=False)

	return invoice_name


def build_sales_order_item_row(plot, warehouse, delivery_date):
	item_code = PLOT_TYPE_TO_ITEM.get(plot.plot_type)
	if not item_code:
		frappe.throw(f"No item is mapped for plot type {plot.plot_type}.")

	item = frappe.db.get_value(
		"Item",
		item_code,
		["name", "item_name", "stock_uom"],
		as_dict=True,
	)
	if not item:
		frappe.throw(f"Item {item_code} was not found.")
	if not item.stock_uom:
		frappe.throw(f"Item {item_code} is missing Stock UOM.")

	return {
		"item_code": item.name,
		"item_name": item.item_name,
		"uom": item.stock_uom,
		"stock_uom": item.stock_uom,
		"conversion_factor": 1,
		"qty": 1,
		"rate": flt(plot.selling_price),
		"warehouse": warehouse,
		"delivery_date": delivery_date,
	}


def build_payment_schedule_rows(total_amount, booking_fee_percent, transaction_date, payment_deadline):
	total_amount = flt(total_amount)
	booking_fee_percent = max(0.0, min(100.0, flt(booking_fee_percent)))
	transaction_date = transaction_date or today()
	payment_deadline = payment_deadline or transaction_date

	if total_amount <= 0:
		return []

	# If no booking fee or both dates are the same, single row
	if booking_fee_percent <= 0 or str(transaction_date) == str(payment_deadline):
		return [{
			"payment_term": "Advance",
			"description": "Full Plot Payment",
			"due_date": payment_deadline,
			"invoice_portion": 100.0,
			"payment_amount": total_amount,
		}]

	booking_amount = flt(total_amount * booking_fee_percent / 100)
	balance_amount = flt(total_amount - booking_amount)
	rows = [{
		"payment_term": "Advance",
		"description": "Advance",
		"due_date": transaction_date,
		"invoice_portion": booking_fee_percent,
		"payment_amount": booking_amount,
	}]

	if balance_amount > 0:
		rows.append({
			"payment_term": "Balance",
			"description": "Balance",
			"due_date": payment_deadline,
			"invoice_portion": flt(100 - booking_fee_percent),
			"payment_amount": balance_amount,
		})

	return rows


def _is_landms_sales_order(doc) -> bool:
	return bool(doc.get("plot_application") or doc.get("plot"))


def _get_application(doc, required=True):
	if not doc.get("plot_application"):
		if required:
			frappe.throw("Plot Application is required for LandMS plot Sales Orders.")
		return None

	if not frappe.db.exists("Plot Application", doc.plot_application):
		if required:
			frappe.throw(f"Plot Application {doc.plot_application} was not found.")
		return None

	app = frappe.get_doc("Plot Application", doc.plot_application)
	if required and (app.docstatus != 1 or app.status not in ("Paid", "Converted")):
		frappe.throw(
			f"Plot Application {app.name} must be submitted and fee-paid before creating a Sales Order."
		)
	return app


def _get_plot(doc):
	if not doc.get("plot"):
		frappe.throw("Plot is required for LandMS Sales Orders.")
	if not frappe.db.exists("Plot Master", doc.plot):
		frappe.throw(f"Plot {doc.plot} was not found.")
	return frappe.get_doc("Plot Master", doc.plot)


def _validate_application_window(application):
	if application.expiry_date and getdate(application.expiry_date) < getdate(today()):
		frappe.throw(
			f"Plot Application {application.name} has expired. The reservation window has ended."
		)


def _validate_plot_state(plot):
	if plot.status not in ("Pending Advance", "Available"):
		frappe.throw(
			f"Plot {plot.name} is not ready for Sales Order creation (current status: {plot.status})."
		)


def _ensure_single_sales_order_for_application(doc, application):
	existing = frappe.db.get_value(
		"Sales Order",
		{
			"plot_application": application.name,
			"name": ("!=", doc.name),
			"docstatus": ("!=", 2),
		},
		"name",
	)
	if existing:
		frappe.throw(
			f"Plot Application {application.name} is already linked to Sales Order {existing}."
		)


def _ensure_items(doc, plot, settings):
	delivery_date = add_days(doc.transaction_date or today(), cint(doc.payment_completion_days or 0))
	row_values = build_sales_order_item_row(plot, settings.plot_inventory_warehouse, delivery_date)

	if len(doc.items or []) == 1 and doc.items[0].get("item_code") == row_values["item_code"]:
		row = doc.items[0]
		for key, value in row_values.items():
			row.set(key, value)
		return

	doc.set("items", [row_values])


def _ensure_payment_schedule(doc, plot):
	schedule_rows = build_payment_schedule_rows(
		total_amount=flt(plot.selling_price),
		booking_fee_percent=flt(doc.booking_fee_percent),
		transaction_date=doc.transaction_date,
		payment_deadline=doc.payment_deadline,
	)

	doc.set("payment_schedule", [])
	for row in schedule_rows:
		doc.append("payment_schedule", row)


def _ensure_draft_plot_contract(doc):
	if doc.get("plot_contract") and frappe.db.exists("Plot Contract", doc.plot_contract):
		return _sync_existing_draft_plot_contract(doc.plot_contract, doc)

	existing = frappe.db.get_value(
		"Plot Contract",
		{"sales_order": doc.name, "docstatus": ("!=", 2)},
		"name",
	)
	if existing:
		return _sync_existing_draft_plot_contract(existing, doc)

	contract = frappe.get_doc({
		"doctype": "Plot Contract",
		"customer": doc.customer,
		"plot": doc.plot,
		"plot_application": doc.get("plot_application") or "",
		"contract_date": doc.transaction_date or today(),
		"payment_completion_days": cint(doc.payment_completion_days or 0),
		"payment_deadline": doc.payment_deadline,
		"apply_auto_cancellation": cint(doc.get("apply_auto_cancellation", 1)),
		"sales_order": doc.name,
		"control_number": doc.get("control_number") or "",
		"booking_fee_percent": flt(doc.booking_fee_percent),
		"government_share_percent": flt(doc.government_share_percent),
		"notes": doc.terms or "",
	})
	_sync_contract_schedule(contract, doc)
	contract.flags.from_sales_order = True
	contract.insert(ignore_permissions=True)
	return contract.name


def _sync_existing_draft_plot_contract(contract_name: str, source_doc) -> str:
	"""Refresh a draft Plot Contract from the Sales Order without failing on harmless races."""
	for attempt in range(2):
		contract = frappe.get_doc("Plot Contract", contract_name)
		if contract.docstatus != 0:
			return contract.name

		if _draft_contract_matches_sales_order(contract, source_doc):
			return contract.name

		try:
			_sync_contract_schedule(contract, source_doc)
			contract.flags.from_sales_order = True
			contract.save(ignore_permissions=True)
			return contract.name
		except frappe.TimestampMismatchError:
			if attempt:
				raise

	return contract_name


def _draft_contract_matches_sales_order(contract, source_doc) -> bool:
	if cstr(contract.control_number or "") != cstr(source_doc.get("control_number") or ""):
		return False
	if cstr(contract.plot_application or "") != cstr(source_doc.get("plot_application") or ""):
		return False
	if cstr(contract.payment_deadline or "") != cstr(source_doc.get("payment_deadline") or ""):
		return False
	if cint(contract.apply_auto_cancellation or 0) != cint(source_doc.get("apply_auto_cancellation", 1)):
		return False

	expected_rows = _build_contract_schedule_rows(source_doc)
	actual_rows = sorted(
		contract.get("payment_schedule") or [],
		key=lambda row: cint(getattr(row, "installment_number", 0) or 0),
	)
	if len(actual_rows) != len(expected_rows):
		return False

	for actual, expected in zip(actual_rows, expected_rows):
		if cint(getattr(actual, "installment_number", 0) or 0) != cint(expected["installment_number"]):
			return False
		if cstr(getattr(actual, "description", "") or "") != cstr(expected["description"] or ""):
			return False
		if cstr(getattr(actual, "due_date", "") or "") != cstr(expected["due_date"] or ""):
			return False
		if flt(getattr(actual, "expected_amount", 0) or 0) != flt(expected["expected_amount"]):
			return False
		if flt(getattr(actual, "paid_amount", 0) or 0) != flt(expected["paid_amount"]):
			return False
		if flt(getattr(actual, "outstanding_amount", 0) or 0) != flt(expected["outstanding_amount"]):
			return False
		if cstr(getattr(actual, "paid_date", "") or "") != cstr(expected["paid_date"] or ""):
			return False
		if cstr(getattr(actual, "sales_invoice", "") or "") != cstr(expected["sales_invoice"] or ""):
			return False
		if cstr(getattr(actual, "status", "") or "") != cstr(expected["status"] or ""):
			return False

	return True


def _build_contract_schedule_rows(source_doc) -> list[dict]:
	rows = []
	for idx, row in enumerate(source_doc.get("payment_schedule") or [], start=1):
		expected_amount = flt(getattr(row, "payment_amount", 0))
		rows.append({
			"installment_number": idx,
			"description": getattr(row, "description", "") or f"Installment {idx}",
			"due_date": row.due_date,
			"expected_amount": expected_amount,
			"paid_amount": 0,
			"outstanding_amount": expected_amount,
			"paid_date": None,
			"sales_invoice": "",
			"status": "Pending",
		})
	return rows


def _sync_contract_schedule(contract, source_doc):
	contract.control_number = source_doc.get("control_number") or ""
	contract.plot_application = source_doc.get("plot_application") or ""
	contract.payment_deadline = source_doc.get("payment_deadline")
	contract.apply_auto_cancellation = cint(source_doc.get("apply_auto_cancellation", 1))
	contract.set("payment_schedule", [])

	for row in _build_contract_schedule_rows(source_doc):
		contract.append("payment_schedule", row)


def _ensure_plot_sales_invoice(doc, contract_name, *, posting_date: str | None = None):
	existing_invoice_name = doc.get("plot_sales_invoice")
	if existing_invoice_name and frappe.db.exists("Sales Invoice", existing_invoice_name):
		_link_plot_invoice_to_sales_order(doc, existing_invoice_name)
		_link_plot_invoice_to_contract(contract_name, existing_invoice_name)
		return existing_invoice_name

	existing = frappe.db.get_value(
		"Sales Invoice",
		{
			"plot": doc.plot,
			"is_plot_sale_invoice": 1,
			"is_return": 0,
			"docstatus": 1,
		},
		"name",
	)
	if existing:
		_link_plot_invoice_to_sales_order(doc, existing)
		_link_plot_invoice_to_contract(contract_name, existing)
		return existing

	from erpnext.selling.doctype.sales_order.sales_order import make_sales_invoice

	posting_date = posting_date or today()
	invoice = make_sales_invoice(doc.name, ignore_permissions=True)
	invoice.posting_date = posting_date
	invoice.due_date = doc.payment_deadline or add_days(
		doc.transaction_date or posting_date, cint(doc.payment_completion_days or 0)
	)
	invoice.ignore_default_payment_terms_template = 1
	invoice.allocate_advances_automatically = 1
	invoice.plot = doc.plot
	invoice.land_acquisition = doc.land_acquisition
	invoice.plot_contract = contract_name or ""
	invoice.is_plot_sale_invoice = 1
	invoice.remarks = f"Plot sale invoice for {doc.plot} via Sales Order {doc.name}"

	# Copy payment schedule from SO
	invoice.set("payment_schedule", [])
	for row in (doc.get("payment_schedule") or []):
		invoice.append("payment_schedule", {
			"payment_term": row.get("payment_term") or row.description,
			"description": row.description,
			"due_date": row.due_date,
			"invoice_portion": flt(row.invoice_portion),
			"payment_amount": flt(row.payment_amount),
		})

	original_user = frappe.session.user
	try:
		frappe.set_user("Administrator")
		invoice.insert(ignore_permissions=True)
		invoice.submit()
	finally:
		frappe.set_user(original_user)

	_link_plot_invoice_to_sales_order(doc, invoice.name)
	_link_plot_invoice_to_contract(contract_name, invoice.name)
	return invoice.name


def _link_plot_invoice_to_sales_order(doc, invoice_name):
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		return

	if doc.get("plot_sales_invoice") != invoice_name:
		frappe.db.set_value("Sales Order", doc.name, "plot_sales_invoice", invoice_name, update_modified=False)

	if not doc.items:
		return

	so_item_name = doc.items[0].name
	if not so_item_name:
		return

	invoice_items = frappe.get_all(
		"Sales Invoice Item",
		filters={"parent": invoice_name},
		fields=["name", "sales_order", "so_detail"],
	)
	for item in invoice_items:
		updates = {}
		if item.sales_order != doc.name:
			updates["sales_order"] = doc.name
		if not item.so_detail:
			updates["so_detail"] = so_item_name
		if updates:
			frappe.db.set_value("Sales Invoice Item", item.name, updates, update_modified=False)


def _link_plot_invoice_to_contract(contract_name, invoice_name):
	if not contract_name or not frappe.db.exists("Plot Contract", contract_name):
		return
	sales_order_name = frappe.db.get_value("Plot Contract", contract_name, "sales_order")
	control_number = ""
	plot_application = ""
	if sales_order_name and frappe.db.exists("Sales Order", sales_order_name):
		so_values = frappe.db.get_value(
			"Sales Order",
			sales_order_name,
			["control_number", "plot_application"],
			as_dict=True,
		)
		control_number = so_values.control_number or ""
		plot_application = so_values.plot_application or ""
	updates = {
		"booking_fee_invoice": invoice_name,
		"control_number": control_number,
		"plot_application": plot_application,
	}
	frappe.db.set_value("Plot Contract", contract_name, updates, update_modified=False)


def _ensure_control_number(doc) -> str:
	current = cstr(doc.get("control_number") or "").strip()
	if current and is_valid_control_number(current):
		return current

	new_cn = generate_control_number(doc.name)
	doc.db_set("control_number", new_cn, update_modified=False)
	doc.control_number = new_cn
	return new_cn


def _block_manual_control_number(doc):
	value = cstr(doc.get("control_number") or "").strip()
	if not value:
		return

	if not doc.is_new() and frappe.db.has_column("Sales Order", "control_number"):
		stored = frappe.db.get_value("Sales Order", doc.name, "control_number")
		if value == cstr(stored or "").strip():
			return

	if not is_valid_control_number(value):
		frappe.throw(
			"TCB Control Number cannot be set manually. It is generated by the "
			"system on Sales Order submit. Clear the field and re-save."
		)


def _create_registry_row(doc, control_number: str):
	create_or_get_registry(
		control_number=control_number,
		sales_order=doc.name,
		customer=doc.customer,
		amount=flt(doc.grand_total),
	)


def _register_with_tcb(doc, control_number: str):
	"""Register the control number with TCB synchronously during SO submit.

	Runs inline so the user gets immediate feedback (success/failure popup)
	instead of waiting for a background worker to pick up the job.
	"""
	register_reference_for_sales_order(doc.name, control_number)


def _link_application_to_sales_order(doc):
	app = _get_application(doc, required=False)
	if app and app.sales_order != doc.name:
		app.db_set("sales_order", doc.name)


def _clear_application_sales_order_link(doc):
	app = _get_application(doc, required=False)
	if app and app.sales_order == doc.name:
		app.db_set("sales_order", "")


def _mark_plot_pending_advance(doc):
	if not doc.plot:
		return
	current = frappe.db.get_value("Plot Master", doc.plot, "status")
	if current == "Available":
		frappe.db.set_value("Plot Master", doc.plot, "status", "Pending Advance")


def _validate_customer_mobile_for_tcb(doc):
	customer = cstr(doc.get("customer") or "").strip()
	if not customer:
		return

	mobile = _get_customer_mobile(customer)
	if is_valid_tcb_mobile(mobile):
		return

	frappe.throw(
		f"Customer {customer} must have a valid phone number in the format "
		"255XXXXXXXXX before this Sales Order can be created. "
		"Update the mobile number on the Customer's primary Contact."
	)


def _get_customer_mobile(customer: str) -> str:
	"""Get mobile from Customer record or its primary Contact."""
	# Try Customer.mobile_no first
	mobile = cstr(frappe.db.get_value("Customer", customer, "mobile_no") or "").strip()
	if mobile:
		return mobile

	# Fall back to primary Contact's mobile_no or phone
	contact = frappe.db.get_value(
		"Dynamic Link",
		{"link_doctype": "Customer", "link_name": customer, "parenttype": "Contact"},
		"parent",
	)
	if contact:
		mobile = cstr(frappe.db.get_value("Contact", contact, "mobile_no") or "").strip()
		if not mobile:
			mobile = cstr(frappe.db.get_value("Contact", contact, "phone") or "").strip()
	return mobile


def _block_cancel_if_paid(doc):
	# Termination flow already handles accounting via forfeiture JE
	if doc.flags.get("_from_termination"):
		return
	plot_invoice = doc.get("plot_sales_invoice")
	if plot_invoice and frappe.db.exists("Sales Invoice", plot_invoice):
		outstanding, grand_total = frappe.db.get_value(
			"Sales Invoice",
			plot_invoice,
			["outstanding_amount", "grand_total"],
		)
		if flt(outstanding) < flt(grand_total):
			frappe.throw(
				f"Sales Order {doc.name} cannot be cancelled — payment has been "
				f"received against Sales Invoice {plot_invoice}. "
				"Use Plot Contract → Terminate Contract instead."
			)


def _cancel_unpaid_plot_sales_invoice(doc):
	# Termination flow already handled the SI before calling SO cancel
	if doc.flags.get("_from_termination"):
		return
	invoice_name = doc.get("plot_sales_invoice")
	if not invoice_name or not frappe.db.exists("Sales Invoice", invoice_name):
		return

	invoice = frappe.get_doc("Sales Invoice", invoice_name)
	if invoice.docstatus == 0:
		frappe.delete_doc("Sales Invoice", invoice.name, ignore_permissions=True)
		return

	if invoice.docstatus != 1:
		return

	if flt(invoice.outstanding_amount) < flt(invoice.grand_total):
		frappe.throw(
			f"Sales Order {doc.name} cannot be cancelled because plot invoice {invoice.name} has payments."
		)

	invoice.cancel()


def _delete_draft_plot_contract(doc):
	# Collect candidates: the stored link on the doc + any DB matches by sales_order.
	candidates = set()
	if doc.get("plot_contract") and frappe.db.exists("Plot Contract", doc.plot_contract):
		candidates.add(doc.plot_contract)

	for name in frappe.db.get_all(
		"Plot Contract",
		filters={"sales_order": doc.name, "docstatus": 0},
		pluck="name",
	):
		candidates.add(name)

	for name in candidates:
		if frappe.db.get_value("Plot Contract", name, "docstatus") == 0:
			frappe.delete_doc("Plot Contract", name, ignore_permissions=True, force=True)


def _release_plot_if_no_active_application(doc):
	if not doc.plot:
		return

	active_application = frappe.db.exists(
		"Plot Application",
		{
			"plot": doc.plot,
			"docstatus": 1,
			"status": ["in", ["Submitted", "Paid", "Converted"]],
		},
	)
	if active_application:
		return

	plot_status = frappe.db.get_value("Plot Master", doc.plot, "status")
	if plot_status in ("Pending Advance", "Pending Fee"):
		frappe.db.set_value("Plot Master", doc.plot, "status", "Available")
