import frappe


def execute():
	"""Give every submitted handover (receipt) a verification code, so a reprint
	carries the QR. Additive only: fills the new, empty verification_code field
	and touches nothing else. Cancelled and draft handovers get none — a draft
	gets its code when it is submitted."""
	from landms.receipt_verification import new_code

	names = frappe.get_all(
		"Plot Handover",
		filters={"docstatus": 1, "verification_code": ["is", "not set"]},
		pluck="name",
	)
	for name in names:
		frappe.db.set_value("Plot Handover", name, "verification_code", new_code(), update_modified=False)
