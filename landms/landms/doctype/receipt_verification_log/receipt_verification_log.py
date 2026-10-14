# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Receipt Verification Log — one row per check from the public verify page
(landms.receipt_verification.verify_receipt). Written by the system only."""

import frappe
from frappe.model.document import Document


class ReceiptVerificationLog(Document):
	def validate(self):
		if not self.is_new():
			frappe.throw("Receipt Verification Log rows cannot be edited.")
