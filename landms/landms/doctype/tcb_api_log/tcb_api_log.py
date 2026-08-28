from frappe.model.document import Document


class TCBAPILog(Document):
	"""Raw audit row for any TCB API call (outbound or inbound).

	Insertion is best-effort — see landms.tcb.create_tcb_api_log which catches
	exceptions so a logging failure can never break the business operation.
	"""

	pass
