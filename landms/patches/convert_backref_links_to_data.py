"""Convert read-only back-reference Link fields to Data fields.

Frappe blocks fieldtype changes from Link → Data via the normal save path.
This patch runs the change directly in SQL so the subsequent custom-field
import (and DocType sync) can proceed without a ValidationError.
"""
import frappe


# Custom Fields (on standard ERPNext doctypes)
CUSTOM_FIELDS = [
    "Sales Order-plot_contract",
    "Sales Order-plot_sales_invoice",
    "Sales Invoice-plot_contract",
]

# DocType fields (on our own doctypes) — stored in tabDocField
DOCTYPE_FIELDS = [
    ("Plot Application", "sales_invoice"),
    ("Plot Application", "payment_entry"),
    ("Plot Application", "sales_order"),
    ("Plot Contract", "sales_order"),
    ("Plot Contract", "plot_application"),
    ("Plot Contract", "booking_fee_invoice"),
    ("Plot Contract", "government_fee_entry"),
    ("Plot Contract", "forfeiture_entry"),
    ("Plot Handover", "delivery_note"),
]


def execute():
    # --- Custom Fields ---
    for cf_name in CUSTOM_FIELDS:
        if frappe.db.exists("Custom Field", cf_name):
            frappe.db.sql(
                "UPDATE `tabCustom Field` SET fieldtype='Data', options='' WHERE name=%s AND fieldtype='Link'",
                (cf_name,),
            )

    # --- DocType fields ---
    for dt, fieldname in DOCTYPE_FIELDS:
        frappe.db.sql(
            "UPDATE `tabDocField` SET fieldtype='Data', options='' WHERE parent=%s AND fieldname=%s AND fieldtype='Link'",
            (dt, fieldname),
        )

    frappe.db.commit()
