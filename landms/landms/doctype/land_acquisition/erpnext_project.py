"""Each project has its own ERPNext Project, one for one (LA-6).

ERPNext's own reports (General Ledger, P&L, Stock Ledger, Receivables) filter by Project, and stock
rows carry the Project but not the Land Acquisition tag, so every Land Acquisition gets one from its
first save: same name, same company, start date from the acquisition date. Nobody fills it by hand.
An amendment is the same project re-done and keeps the same ERPNext Project. Documents get it beside
their Land Acquisition tag automatically (landms/overrides/project_tag.py).
"""

import frappe
from frappe import _


def ensure_erpnext_project(doc, method=None):
	"""Project hook (on_update): the project has its ERPNext Project; a draft's new name is carried over."""
	name = doc.erpnext_project or (
		doc.amended_from and frappe.db.get_value("Land Acquisition", doc.amended_from, "erpnext_project")
	)
	cost_center = _cost_center_of(doc.name)
	if name and frappe.db.exists("Project", name):
		project_name, status, current = frappe.db.get_value(
			"Project", name, ["project_name", "status", "cost_center"]
		)
		changes = {}
		if project_name != doc.acquisition_name:
			_refuse_if_taken(doc.acquisition_name, but_not=name)
			changes["project_name"] = doc.acquisition_name
		if status == "Cancelled":
			changes["status"] = "Open"
		if cost_center and current != cost_center:
			changes["cost_center"] = cost_center
		if changes:
			frappe.db.set_value("Project", name, changes)
	else:
		_refuse_if_taken(doc.acquisition_name)
		project = frappe.get_doc(
			{
				"doctype": "Project",
				"project_name": doc.acquisition_name,
				"company": doc.company,
				"expected_start_date": doc.acquisition_date,
				"cost_center": cost_center,
			}
		)
		# a land project runs for years and has no end date; live requires one on Projects made by hand
		# (Property Setter, 1 Aug 2026), so this one is created without it
		project.insert(ignore_permissions=True, ignore_mandatory=True)
		project.add_comment("Comment", _("Created with project {0}.").format(doc.name))
		name = project.name
	if doc.erpnext_project != name:
		doc.db_set("erpnext_project", name, update_modified=False)


def cancel_erpnext_project(doc, method=None):
	"""Project hook (on_cancel): its ERPNext Project is marked Cancelled, never deleted (postings may point at it)."""
	if doc.erpnext_project:
		frappe.db.set_value("Project", doc.erpnext_project, "status", "Cancelled")


def delete_erpnext_project(doc, method=None):
	"""Project hook (after_delete, once the project itself is gone): its ERPNext Project goes with it,
	unless a document points at that Project; then it is only marked Cancelled."""
	if not doc.erpnext_project or not frappe.db.exists("Project", doc.erpnext_project):
		return
	try:
		frappe.delete_doc("Project", doc.erpnext_project, ignore_permissions=True)
	except frappe.LinkExistsError:
		frappe.clear_last_message()
		frappe.db.set_value("Project", doc.erpnext_project, "status", "Cancelled")


def sync_project_cost_center(settings, method=None):
	"""Settings hook (on_update): the ERPNext Project's cost centre is the one Finance set in the project's
	own settings, never the site-wide LandMS Settings; ERPNext then offers it on the project's lines."""
	name = frappe.db.get_value("Land Acquisition", settings.land_acquisition, "erpnext_project")
	if (
		name
		and settings.cost_center
		and frappe.db.get_value("Project", name, "cost_center") != settings.cost_center
	):
		frappe.db.set_value("Project", name, "cost_center", settings.cost_center)


def _cost_center_of(land_acquisition):
	"""The cost centre in the project's own settings (LA-7); none until Finance fills it."""
	if not frappe.db.exists("DocType", "LandMS Project Settings"):
		return None
	return frappe.db.get_value("LandMS Project Settings", land_acquisition, "cost_center")


def _refuse_if_taken(project_name, but_not=None):
	"""ERPNext Project names are unique: another Project with this name stops the save."""
	if frappe.db.exists("Project", {"project_name": project_name, "name": ["!=", but_not or ""]}):
		frappe.throw(
			_("An ERPNext Project named {0} already exists. Rename this project.").format(project_name)
		)
