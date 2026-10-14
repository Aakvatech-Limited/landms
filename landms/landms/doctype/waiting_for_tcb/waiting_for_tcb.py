# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Waiting for TCB — one TCB step that could not be done while TCB was down.
Rows are added only by the code (landms.payments.tcb_catch_up.add); people use
the two buttons: Try now, and Don't do this."""

import frappe
from frappe import _
from frappe.model.document import Document

from landms.payments import tcb_catch_up


class WaitingforTCB(Document):
	@frappe.whitelist(methods=["POST"])  # changes data / calls Malipopay or TCB
	def try_now(self):
		"""Form button: check this step again and, if still needed, do it at TCB now."""
		frappe.only_for(tcb_catch_up.ROLES)
		outcome = tcb_catch_up.run_entry(self.name, by=frappe.session.user, manual=True)
		row = frappe.db.get_value(
			tcb_catch_up.QUEUE, self.name, ["status", "last_result", "closed_note"], as_dict=True
		)
		message = {
			"done": _("Done at TCB."),
			"dropped": _("Not needed any more: {0}").format(row.closed_note),
			"failed": _("TCB did not do it: {0}").format(row.last_result),
			"skipped": _("This entry is {0}; nothing to do.").format(row.status),
		}.get(outcome, row.last_result or "")
		return {"ok": outcome in ("done", "dropped"), "outcome": outcome, "message": message}

	@frappe.whitelist(methods=["POST"])  # changes data / calls Malipopay or TCB
	def stop(self, note=None):
		"""Form button "Don't do this": a person decides the step is not wanted."""
		frappe.only_for(tcb_catch_up.ROLES)
		if self.status not in ("Waiting", "Needs a Person"):
			frappe.throw(_("This entry is already {0}.").format(self.status))
		tcb_catch_up._close(
			self.name,
			"Dropped",
			_("Stopped by {0}: {1}").format(frappe.session.user, note or _("no reason given")),
		)
		return {"ok": True, "message": _("Stopped — nothing will be sent to TCB for this entry.")}
