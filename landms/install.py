import frappe


def after_install():
	_create_land_acquisition_dimension()
	_create_land_acquisition_workflow()


def _create_land_acquisition_workflow():
	"""Create the approval workflow for Land Acquisition.

	Idempotent — safe to run multiple times. Frappe ships only a few
	Workflow State / Workflow Action Master records, so we create the
	missing ones first, then the Workflow itself. The workflow drives the
	`status` field (read-only on the form) from Draft → Pending Approval →
	Approved, which is what `LandAcquisition.before_submit` checks.
	"""
	# 1. Workflow States — ship by default: Approved, Pending, Rejected.
	#    We need: Draft, Pending Approval, Subdivided.
	state_styles = {
		"Draft":            "Danger",
		"Pending Approval": "Warning",
		"Approved":         "Success",
		"Subdivided":       "Primary",
	}
	for state, style in state_styles.items():
		if not frappe.db.exists("Workflow State", state):
			frappe.get_doc({
				"doctype": "Workflow State",
				"workflow_state_name": state,
				"style": style,
			}).insert(ignore_permissions=True)

	# 2. Workflow Action Masters — ship by default: Approve, Reject, Review.
	#    We need: Submit for Approval.
	for action in ["Submit for Approval"]:
		if not frappe.db.exists("Workflow Action Master", action):
			frappe.get_doc({
				"doctype": "Workflow Action Master",
				"workflow_action_name": action,
			}).insert(ignore_permissions=True)

	# 3. The workflow itself.
	if frappe.db.exists("Workflow", "Land Acquisition Approval"):
		return

	frappe.get_doc({
		"doctype": "Workflow",
		"workflow_name": "Land Acquisition Approval",
		"document_type": "Land Acquisition",
		"is_active": 1,
		"send_email_alert": 0,
		"workflow_state_field": "status",
		"states": [
			{"state": "Draft",            "doc_status": "0", "allow_edit": "Land Acquisition Approver"},
			{"state": "Pending Approval", "doc_status": "0", "allow_edit": "Land Acquisition Approver"},
			{"state": "Approved",         "doc_status": "1", "allow_edit": "Land Acquisition Approver"},
			{"state": "Subdivided",       "doc_status": "1", "allow_edit": "Land Acquisition Approver"},
		],
		"transitions": [
			# Draft → Pending Approval (anyone with write access)
			{"state": "Draft", "action": "Submit for Approval", "next_state": "Pending Approval",
			 "allowed": "Land Acquisition Approver", "allow_self_approval": 1},
			{"state": "Draft", "action": "Submit for Approval", "next_state": "Pending Approval",
			 "allowed": "System Manager", "allow_self_approval": 1},
			# Pending Approval → Approved
			{"state": "Pending Approval", "action": "Approve", "next_state": "Approved",
			 "allowed": "Land Acquisition Approver", "allow_self_approval": 1},
			{"state": "Pending Approval", "action": "Approve", "next_state": "Approved",
			 "allowed": "System Manager", "allow_self_approval": 1},
			# Pending Approval → Draft (reject)
			{"state": "Pending Approval", "action": "Reject", "next_state": "Draft",
			 "allowed": "Land Acquisition Approver", "allow_self_approval": 1},
			{"state": "Pending Approval", "action": "Reject", "next_state": "Draft",
			 "allowed": "System Manager", "allow_self_approval": 1},
		],
	}).insert(ignore_permissions=True)
	frappe.db.commit()


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
