"""Journal Entry doc-event hooks for LandMS."""

import frappe


def before_save_journal_entry(doc, method=None):
	"""Auto-fill cost_center from LandMS Settings on rows that have
	land_acquisition set but are missing a cost_center."""

	# Find the cost_center from settings (cached single-doc read)
	cost_center = frappe.db.get_single_value("LandMS Settings", "cost_center")
	if not cost_center:
		return

	for row in (doc.accounts or []):
		if row.get("land_acquisition"):
			row.cost_center = cost_center
