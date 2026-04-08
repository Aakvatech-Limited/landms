import frappe
from frappe.model.document import Document


class TCBPaymentNotification(Document):
	"""Structured record of an inbound TCB IPN callback.

	One row per unique TCB transaction_id. The IPN handler in
	landms.api.tcb.ipn_callback is the only writer in the normal flow;
	users may re-open a Failed row and re-run processing manually.
	"""

	pass
