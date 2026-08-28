import frappe


def execute():
	if frappe.db.exists("Role", "Land Acquisition Approver"):
		return

	frappe.get_doc(
		{
			"doctype": "Role",
			"role_name": "Land Acquisition Approver",
			"desk_access": 1,
			"is_custom": 1,
		}
	).insert(ignore_permissions=True)

	frappe.db.commit()
