"""Backfill Land Acquisition.payment_channel = "TCB" for every existing project.

landms is live on TCB only. The new ``payment_channel`` Select carries a default
of "TCB", but existing rows created before the field existed can hold NULL. This
patch sets them all to "TCB" so channel resolution
(``landms.payments.registry.channel_for_land_acquisition``) always finds a real
value and never has to fall back. Idempotent — safe to re-run.
"""

import frappe


def execute():
	if not frappe.db.has_column("Land Acquisition", "payment_channel"):
		# field not created yet (create_custom_fields / model sync runs first);
		# nothing to backfill.
		return

	frappe.db.sql(
		"""
        UPDATE `tabLand Acquisition`
        SET payment_channel = 'TCB'
        WHERE payment_channel IS NULL OR payment_channel = ''
        """
	)
	frappe.db.commit()
