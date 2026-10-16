"""Cost centre for LandMS documents — one rule, in one place.

Every document the app writes (plot Sales Order, plot Sales Invoice and its
credit note, Payment Entry against a plot, handover Delivery Note, and the
government-share / revenue / forfeiture / termination Journal Entries) posts
to the cost centre held in LandMS Settings. The builders have always set it
on the LINES; the HEADER fields — `cost_center` on Sales Order, Sales Invoice,
Payment Entry and Delivery Note, and the live site's `custom_cost_center` on
Journal Entry — were never filled. When those headers were made mandatory on
the live site (Customize Form, 2026-09-06/07) Plot Application saves and TCB
money recording broke, and five "Plot cost center (X)" Server Scripts were
added as guards. This module is those guards, inside the app, so they can
retire:

  * fills ONLY empty fields — a human's or another app's choice is never
    overridden;
  * acts ONLY on documents that are demonstrably LandMS's (see the
    per-doctype tests below) AND belong to the LandMS company — every other
    document on the site is left exactly as it was;
  * never raises — a missing setting or an unexpected shape is logged and
    the document proceeds unchanged. A cost-centre convenience must not be
    able to block a sale or a payment.

Wired in hooks.py as `before_validate` on the five doctypes: that runs on
every save path (builders, UI, console, IPN worker) and before Frappe's
mandatory-field check, which is the check the live site now enforces.
"""

from __future__ import annotations

import frappe
from frappe.utils import cint


def apply_landms_cost_center(doc, method=None):
	"""before_validate hook — fill the cost centre from LandMS Settings on LandMS
	documents only. Safe to call on any document of the wired doctypes."""
	try:
		settings = frappe.get_cached_doc("LandMS Settings")
		cost_center = settings.get("cost_center")
		if not cost_center:
			return
		# Other companies on the same site are none of our business.
		if doc.get("company") and settings.get("company") and doc.company != settings.company:
			return
		is_ours = _IS_LANDMS.get(doc.doctype)
		if not is_ours or not is_ours(doc):
			return
		# ERPNext pre-fills item rows (and may pre-fill headers) with the
		# company's default cost centre in before_insert — before this hook
		# runs. On a LandMS document that value is the framework's automatic
		# fill, not a person's decision, so it counts as unset. A specific cost
		# centre chosen by a human is always kept.
		company_default = (
			frappe.get_cached_value("Company", settings.company, "cost_center")
			if settings.get("company")
			else None
		)

		def unset(value):
			return (not value) or bool(company_default and value == company_default)

		_fill_header(doc, cost_center, unset)
		_fill_rows(doc, cost_center, unset)
	except Exception:
		frappe.log_error(frappe.get_traceback(), "LandMS cost centre fill skipped")


# ---------------------------------------------------------------------------
# "Is this document LandMS's?" — one test per doctype, all read-only
# ---------------------------------------------------------------------------


def _so_is_landms(sales_order_name) -> bool:
	if not sales_order_name:
		return False
	row = frappe.db.get_value(
		"Sales Order",
		sales_order_name,
		["plot_application", "plot", "land_acquisition"],
		as_dict=True,
	)
	return bool(row and (row.plot_application or row.plot or row.land_acquisition))


def _si_is_landms(sales_invoice_name) -> bool:
	return bool(
		sales_invoice_name
		and cint(frappe.db.get_value("Sales Invoice", sales_invoice_name, "is_plot_sale_invoice"))
	)


def _is_landms_sales_order(doc) -> bool:
	return bool(doc.get("plot_application") or doc.get("plot") or doc.get("land_acquisition"))


def _is_landms_sales_invoice(doc) -> bool:
	if cint(doc.get("is_plot_sale_invoice")):
		return True
	if cint(doc.get("is_return")) and _si_is_landms(doc.get("return_against")):
		return True  # credit note raised on a plot invoice
	return any(_so_is_landms(row.get("sales_order")) for row in (doc.get("items") or []))


def _is_landms_payment_entry(doc) -> bool:
	if doc.get("land_acquisition"):
		return True
	for row in doc.get("references") or []:
		if row.get("reference_doctype") == "Sales Invoice" and _si_is_landms(row.get("reference_name")):
			return True
		if row.get("reference_doctype") == "Sales Order" and _so_is_landms(row.get("reference_name")):
			return True
	return False


def _is_landms_delivery_note(doc) -> bool:
	for row in doc.get("items") or []:
		if row.get("land_acquisition") or _so_is_landms(row.get("against_sales_order")):
			return True
	return False


def _is_landms_journal_entry(doc) -> bool:
	if doc.get("lms_payment_entry"):
		return True
	return any(row.get("land_acquisition") for row in (doc.get("accounts") or []))


_IS_LANDMS = {
	"Sales Order": _is_landms_sales_order,
	"Sales Invoice": _is_landms_sales_invoice,
	"Payment Entry": _is_landms_payment_entry,
	"Delivery Note": _is_landms_delivery_note,
	"Journal Entry": _is_landms_journal_entry,
}


# ---------------------------------------------------------------------------
# The fill — empty fields only
# ---------------------------------------------------------------------------


def _fill_header(doc, cost_center, unset):
	if doc.doctype == "Journal Entry":
		# Journal Entry has no standard header cost centre; the live site carries
		# a Customize-Form field for it. Fill it only where that field exists.
		if doc.meta.has_field("custom_cost_center") and unset(doc.get("custom_cost_center")):
			doc.custom_cost_center = cost_center
		return
	if doc.meta.has_field("cost_center") and unset(doc.get("cost_center")):
		doc.cost_center = cost_center


def _fill_rows(doc, cost_center, unset):
	if doc.doctype == "Journal Entry":
		# Rows tagged to a Land Acquisition are LandMS's and post to its cost
		# centre (the long-standing before_save rule, kept verbatim).
		for row in doc.get("accounts") or []:
			if row.get("land_acquisition"):
				row.cost_center = cost_center
		return
	if doc.doctype == "Payment Entry":
		return  # header only — a Payment Entry has no cost-centre rows
	for row in doc.get("items") or []:
		if unset(row.get("cost_center")):
			row.cost_center = cost_center
