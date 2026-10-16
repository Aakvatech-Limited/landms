# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Malipopay Webhook Event — the durable inbound inbox.

Malipopay attempts delivery exactly ONCE (no retry). The webhook endpoint
therefore persists this row and commits BEFORE answering 200; from that moment
a worker crash, Redis outage, or code bug can only delay processing — never
lose the payment. The repair job re-processes rows left Pending/Failed.

Named MPW-<transactionId> so duplicate deliveries collide at the database
(DuplicateEntryError → answered as a duplicate), not in racy application code.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now


class MalipopayWebhookEvent(Document):
	def before_insert(self):
		if not self.received_at:
			self.received_at = now()
