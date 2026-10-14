# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Malipopay API Log — request/response audit for the Malipopay rail.

A LOG, not an outbox: rows may eventually be pruned; nothing operational may
depend on their presence. (The Phase-2 outbox, when Malipopay ships expire /
external-receipt, will be its own doctype with opposite retention rules.)

IMPORTANT — deliberately unlike TCB's create_tcb_api_log: the Malipopay logger
must NEVER commit mid-transaction. The money door commits once, at the end;
logging happens after the fact or in its own background context. The TCB
logger's embedded commit is the reason the TCB gates had to move into
before_submit/before_cancel — that constraint stays out of this rail.
"""

from frappe.model.document import Document


class MalipopayAPILog(Document):
	pass
