import frappe
from landms.landms.doctype.land_acquisition.land_acquisition import sync_land_acquisition_cost_summary


def execute():
	"""Recalculate all submitted Land Acquisition costs to include Journal Entry amounts.

	Deployed when JE cost sync was added. Runs once at bench migrate so all
	existing LAs reflect the correct cost (PI + JE) before the hook takes over
	for future changes.
	"""
	la_names = frappe.db.get_all(
		"Land Acquisition",
		filters={"docstatus": 1},
		pluck="name",
	)

	for name in la_names:
		try:
			sync_land_acquisition_cost_summary(name)
			frappe.db.commit()
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"LA cost recalc failed: {name}")
