"""Backfill Land Acquisition.collect_via_tcb = 1 for every existing project.

All live projects collect through TCB today (the legacy payment_channel field
says 'TCB' everywhere). The new per-channel checkbox defaults to 1 for NEW
documents, but existing rows have NULL — without this backfill their TCB
settings section would hide and the conditional-mandatory rules would relax.
"""

import frappe


def execute():
	if not frappe.db.has_column("Land Acquisition", "collect_via_tcb"):
		return
	frappe.db.sql(
		"""
        UPDATE `tabLand Acquisition`
        SET collect_via_tcb = 1
        WHERE IFNULL(collect_via_tcb, 0) = 0
          AND (IFNULL(payment_channel, '') = 'TCB' OR IFNULL(partner_code, '') != '')
        """
	)
