import json
import os
import sys

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

FOLDER = "custom_fields_json"

DISALLOWED_FIELDS = {
	"name", "owner", "creation", "modified", "modified_by",
	"docstatus", "idx", "is_system_generated",
}


def _load_json(filepath):
	with open(filepath, "r") as f:
		return json.load(f)


def _build_fields_dict(custom_fields_list):
	valid_columns = set(frappe.get_meta("Custom Field").get_valid_columns())
	allowed = valid_columns - DISALLOWED_FIELDS

	result = {}
	for cf in custom_fields_list:
		doctype = cf.get("dt")
		if not doctype:
			continue
		field_dict = {k: cf[k] for k in allowed if k in cf}
		result.setdefault(doctype, []).append(field_dict)

	for doctype in result:
		result[doctype].sort(key=lambda f: 1 if f.get("fieldtype") == "Dynamic Link" else 0)

	return result


def execute():
	curr_dir = os.path.abspath(os.path.dirname(__file__))
	json_dir = os.path.join(curr_dir, FOLDER)

	files = sorted(f for f in os.listdir(json_dir) if f.endswith(".json"))

	for filename in files:
		filepath = os.path.join(json_dir, filename)
		try:
			data = _load_json(filepath)
			fields_dict = _build_fields_dict(data)
			try:
				create_custom_fields(fields_dict, update=True)
			except Exception:
				for doctype, fields in fields_dict.items():
					for df in fields:
						fieldname = df.get("fieldname", "unknown")
						try:
							create_custom_fields({doctype: [df]}, update=True)
						except Exception as e:
							print(
								f"WARNING [landms]: Failed to create custom field "
								f"'{fieldname}' on '{doctype}': {e}",
								file=sys.stderr,
							)
							frappe.log_error(
								title=f"landms: custom field failed - {doctype}.{fieldname}",
								message=frappe.get_traceback(),
							)
		except Exception as e:
			print(
				f"WARNING [landms]: Failed to process '{filename}': {e}",
				file=sys.stderr,
			)
			frappe.log_error(
				title=f"landms: custom field file failed - {filename}",
				message=frappe.get_traceback(),
			)
