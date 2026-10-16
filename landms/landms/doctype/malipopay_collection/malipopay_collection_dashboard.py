from frappe import _


def get_data():
	"""Connections tab: everything that happened to this bill, the Malipopay
	counterpart of the event trail on TCB Control Number."""
	return {
		"fieldname": "malipopay_collection",
		"transactions": [
			{"label": _("Calls and Webhooks"), "items": ["Malipopay API Log", "Malipopay Webhook Event"]},
			{"label": _("Money"), "items": ["Payment Receipt Ledger", "Malipopay External Entry"]},
		],
	}
