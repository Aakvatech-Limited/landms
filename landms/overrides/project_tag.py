"""The ERPNext Project always follows the Land Acquisition tag (LA-6).

Wired (hooks.py) as the last validate step of the document types LandMS tags: orders, invoices,
payments, journals, stock entries, deliveries, supplier orders and invoices. On those, wherever a
Land Acquisition tag is set (on the document or on a line), the Project box gets that project's
ERPNext Project; a different value is replaced, so the two tags never disagree. A document with no
tag is left exactly as it was, and this rule never stops a save: anything unexpected is logged.
ERPNext then carries the Project onto every ledger and stock row by itself.
"""

import frappe
from frappe import _


def stamp_project(doc, method=None):
	"""validate hook: put the project's ERPNext Project beside every Land Acquisition tag."""
	try:
		replaced = _stamp(doc)
	except Exception:
		frappe.log_error(title=f"LandMS: project not stamped on {doc.doctype} {doc.name}")
		return
	if replaced:
		frappe.msgprint(
			_("Project set to {0}, as the Land Acquisition says.").format(", ".join(sorted(replaced))),
			indicator="orange",
			alert=True,
		)


def _stamp(doc):
	"""Stamp the document and its lines; return the projects that were set over a different value."""
	meta = frappe.get_meta(doc.doctype)
	tag = doc.get("land_acquisition") if meta.has_field("land_acquisition") else None
	rows = [(doc, tag)] if tag and meta.has_field("project") else []
	for table in meta.get_table_fields():
		line_meta = frappe.get_meta(table.options)
		if not line_meta.has_field("project"):
			continue
		tagged_lines = line_meta.has_field("land_acquisition")
		for line in doc.get(table.fieldname) or []:
			line_tag = (line.get("land_acquisition") if tagged_lines else None) or tag
			if line_tag:
				rows.append((line, line_tag))

	projects, replaced = {}, set()
	for row, land_acquisition in rows:
		if land_acquisition not in projects:
			projects[land_acquisition] = frappe.db.get_value(
				"Land Acquisition", land_acquisition, "erpnext_project"
			)
		project = projects[land_acquisition]
		if project and row.get("project") != project:
			if row.get("project"):
				replaced.add(project)
			row.project = project
	return replaced
