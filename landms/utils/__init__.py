import os
import json

import frappe


def import_setup_data():
	"""Import JSON data files from the utils folder after migrate.

	Called via after_migrate hook. Handles insert-or-update for each doc
	and uses fieldname-based matching for Custom Fields to avoid duplicates.
	"""
	utils_dir = os.path.dirname(__file__)

	# Order matters: roles first, then fields that reference them.
	files_to_import = [
		"role.json",
		"payment_term.json",
		"custom_field.json",
		"client_script.json",
	]

	for filename in files_to_import:
		filepath = os.path.join(utils_dir, filename)
		if not os.path.exists(filepath):
			continue

		with open(filepath, "r") as f:
			data = json.load(f)

		if not isinstance(data, list):
			data = [data]

		for doc_data in data:
			doctype = doc_data.get("doctype")
			if not doctype:
				continue

			_upsert_doc(doctype, doc_data)

	frappe.db.commit()


def _upsert_doc(doctype, doc_data):
	"""Insert or update a single doc, with special handling for Custom Fields."""
	existing_name = None

	if doctype == "Custom Field":
		# Match by parent doctype + fieldname — more reliable than the
		# auto-generated name which can differ across sites.
		dt = doc_data.get("dt")
		fieldname = doc_data.get("fieldname")
		if dt and fieldname:
			existing_name = frappe.db.get_value(
				"Custom Field", {"dt": dt, "fieldname": fieldname}, "name"
			)
	elif doctype == "Client Script":
		dt = doc_data.get("dt")
		script_name = doc_data.get("name")
		if dt:
			existing_name = frappe.db.get_value(
				"Client Script", {"dt": dt, "name": script_name}, "name"
			)
	else:
		name = doc_data.get("name")
		if name and frappe.db.exists(doctype, name):
			existing_name = name

	if existing_name:
		existing = frappe.get_doc(doctype, existing_name)
		existing.update(doc_data)
		existing.flags.ignore_permissions = True
		existing.save()
	else:
		doc = frappe.get_doc(doc_data)
		doc.flags.ignore_permissions = True
		doc.insert()
