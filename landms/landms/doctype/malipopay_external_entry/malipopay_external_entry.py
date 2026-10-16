# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Malipopay External Entry — one row per CREDIT/DEBIT reported to Malipopay
under section 11 of their v1.1 contract (POST /api/v1/ledger/external-entries).

A row is written BEFORE the call is made, so a retry always resends the same
external_reference and Malipopay answers with a replay instead of posting the
money twice. The facts of a row never change after insert; only the delivery
fields (status, attempts, response) do. The engine is landms/malipopay_external.py.
"""

import frappe
from frappe import _
from frappe.model.document import Document

_IMMUTABLE_FIELDS = (
	"entry_type",
	"amount",
	"external_reference",
	"malipopay_collection",
	"payment_entry",
	"ml_reference",
)


class MalipopayExternalEntry(Document):
	def validate(self):
		if self.amount is None or int(self.amount) <= 0:
			frappe.throw(_("Amount must be a positive whole number of shillings."))
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		for field in _IMMUTABLE_FIELDS:
			if (before.get(field) or None) != (self.get(field) or None):
				frappe.throw(
					_("{0} cannot change after the entry is written.").format(self.meta.get_label(field))
				)

	def on_trash(self):
		if self.status in ("Sent", "Replayed"):
			frappe.throw(_("An entry Malipopay has accepted cannot be deleted; report a DEBIT instead."))
