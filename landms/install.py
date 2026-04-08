import frappe


def after_install():
	_create_land_acquisition_dimension()


def _create_land_acquisition_dimension():
	"""Create the Land Acquisition accounting dimension once on install.

	Idempotent — safe to call multiple times.
	When created, ERPNext automatically adds the land_acquisition field to
	GL Entry, Journal Entry, Sales Invoice, Purchase Invoice, Payment Entry, etc.
	The dimension is NOT mandatory — only land-related transactions tag it.
	"""
	if frappe.db.exists("Accounting Dimension", "Land Acquisition"):
		return

	try:
		dim = frappe.get_doc({
			"doctype": "Accounting Dimension",
			"document_type": "Land Acquisition",
			"label": "Land Acquisition",
			"fieldname": "land_acquisition",
			"disabled": 0,
			"mandatory_for_bs": 0,
			"mandatory_for_pl": 0,
		})
		dim.insert(ignore_permissions=True)
		frappe.db.commit()
	except Exception:
		frappe.log_error(frappe.get_traceback(), "LandMS: Failed to create accounting dimension")
