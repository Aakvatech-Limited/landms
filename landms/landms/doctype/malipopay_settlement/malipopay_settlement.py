# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Malipopay Settlement — Malipopay's cut of one payment, as a record only.

Created by the system for every Malipopay payment once it is booked, from
Malipopay's own figures in the bill breakdown: what the customer paid, the
cut Malipopay posted for it in its ledger (or, when that line is not there
yet, the payment x its percentage, feePercent) and what is left. No
accounting entry is made (operator, 14 Oct 2026): the records are kept until
the agreement with Malipopay says what to do with them.
"""

from frappe.model.document import Document


class MalipopaySettlement(Document):
	pass
