import frappe


def execute():
	terms = [
		{
			"doctype": "Payment Term",
			"payment_term_name": "Advance",
			"description": "Booking fee / advance installment",
		},
		{
			"doctype": "Payment Term",
			"payment_term_name": "Balance",
			"description": "Remaining balance after advance",
		},
	]

	for term in terms:
		if frappe.db.exists("Payment Term", term["payment_term_name"]):
			continue

		frappe.get_doc(term).insert(ignore_permissions=True)

	frappe.db.commit()
