# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Payment Receipt Ledger — the immutable cross-rail receipt spine.

One row per (source rail, provider transaction id). This is:
- the cross-rail idempotency source ("have we seen this money before?") —
  independent of Payment Entry docstatus, so a surgically-cancelled PE can
  never cause the same provider transaction to be re-booked automatically;
- the stable externalTxnId store served to Malipopay's reference_status poll
  (survives PE cancel / amend / rename);
- the audit trail that ties a provider statement line to a GL entry.

Immutability rules enforced here: rows are never deleted, and the identifying
facts (rail, txn id, reference, amount) never change after insert. The ONLY
sanctioned mutation is the status enum (e.g. Booked → 'Voided by Operator'
during a supervised reversal surgery) and link/note fields.
"""

import frappe
from frappe import _
from frappe.model.document import Document

# Fields that are facts, frozen at insert.
_IMMUTABLE_FIELDS = ("source_rail", "txn_id", "reference", "amount", "paid_at", "channel")

# Status may only move along these edges; every state still counts as "seen"
# for dedupe purposes — that is the whole point of the ledger.
_ALLOWED_STATUS_TRANSITIONS = {
	"Booked": {"Voided by Operator"},
	"Suspense": {"Booked", "Voided by Operator", "Suppressed"},
	"Suppressed": {"Booked"},
	"Voided by Operator": set(),
}


class PaymentReceiptLedger(Document):
	def validate(self):
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before:
			return
		for field in _IMMUTABLE_FIELDS:
			if (before.get(field) or "") != (self.get(field) or "") and str(before.get(field) or "") != str(
				self.get(field) or ""
			):
				frappe.throw(
					_(
						"Payment Receipt Ledger rows are immutable facts — {0} cannot be "
						"changed after insert. Corrections are made by changing Status, "
						"never by editing the receipt."
					).format(frappe.bold(field))
				)
		if before.status != self.status:
			allowed = _ALLOWED_STATUS_TRANSITIONS.get(before.status, set())
			if self.status not in allowed:
				frappe.throw(
					_("Illegal receipt status transition: {0} → {1}. Allowed from {0}: {2}.").format(
						before.status, self.status, sorted(allowed) or _("none (terminal)")
					)
				)

	def on_trash(self):
		frappe.throw(
			_(
				"Payment Receipt Ledger rows are never deleted — they are the "
				"idempotency and audit spine for rail payments. Void the receipt "
				"(Status → 'Voided by Operator') instead."
			)
		)
