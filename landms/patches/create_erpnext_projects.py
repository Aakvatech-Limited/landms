"""LA-6: every project that is not cancelled gets its ERPNext Project (on live: BUIGIRI-CHAMWINO).

Additive only: the Project is created with the project's name, company, start date and the cost
centre from its own settings; no document, ledger row or stock row is changed (old rows are a
question for the accountant). A project whose name is already taken by another ERPNext Project is
skipped and logged, never fails the deploy.
"""

import frappe

from landms.landms.doctype.land_acquisition.erpnext_project import ensure_erpnext_project


def execute():
	projects = frappe.get_all(
		"Land Acquisition",
		{"docstatus": ["<", 2], "erpnext_project": ["is", "not set"]},
		pluck="name",
		order_by="creation",
	)
	for name in projects:
		try:
			ensure_erpnext_project(frappe.get_doc("Land Acquisition", name))
		except frappe.ValidationError:
			frappe.log_error(title=f"LandMS: no ERPNext Project for {name}")
