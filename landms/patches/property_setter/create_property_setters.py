import json
import os
import sys

import frappe
from frappe.custom.doctype.property_setter.property_setter import make_property_setter

FOLDER = "property_setters_json"

DISALLOWED_FIELDS = {
	"name", "owner", "creation", "modified", "modified_by",
	"docstatus", "idx", "is_system_generated", "__last_sync_on",
}


def _load_json(filepath):
	with open(filepath, "r") as f:
		return json.load(f)


def _apply_property_setters(property_setters_list):
	existing = {d.name for d in frappe.db.get_all("Property Setter", fields=["name"], page_length=10000)}

	valid_columns = set(frappe.get_meta("Property Setter").get_valid_columns())
	allowed = valid_columns - DISALLOWED_FIELDS

	for ps in property_setters_list:
		if ps.get("name") in existing:
			continue

		ps_dict = {k: ps[k] for k in allowed if k in ps}
		for_doctype = ps.get("doctype_or_field") == "DocType"

		try:
			make_property_setter(
				doctype=ps_dict.get("doc_type"),
				fieldname=ps_dict.get("field_name") or None,
				property=ps_dict.get("property"),
				value=ps_dict.get("value"),
				property_type=ps_dict.get("property_type"),
				for_doctype=for_doctype,
			)
		except Exception:
			ps_name = ps.get("name", "unknown")
			print(
				f"WARNING [landms]: Failed to create property setter '{ps_name}'",
				file=sys.stderr,
			)
			frappe.log_error(
				title=f"landms: property setter failed - {ps_name}",
				message=frappe.get_traceback(),
			)


def execute():
	curr_dir = os.path.abspath(os.path.dirname(__file__))
	json_dir = os.path.join(curr_dir, FOLDER)

	files = sorted(f for f in os.listdir(json_dir) if f.endswith(".json"))

	for filename in files:
		filepath = os.path.join(json_dir, filename)
		try:
			data = _load_json(filepath)
			_apply_property_setters(data)
		except Exception:
			print(
				f"WARNING [landms]: Failed to process property setters from '{filename}'",
				file=sys.stderr,
			)
			frappe.log_error(
				title=f"landms: property setter file failed - {filename}",
				message=frappe.get_traceback(),
			)
