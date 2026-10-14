# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Malipopay Sweep Log — one row per reconciliation-sweep run.

Twin of TCB Reconciliation Log, with two extra duties:
- its child rows ARE the daily three-way divergence report (their ledger vs
  our Payment Receipt Ledger vs the GL) — nonzero divergences alert the
  operator rather than sleeping in a log;
- the sweep watermark (Malipopay Settings.sweep_watermark) advances ONLY when
  a run completes successfully, so a failed run leaves the window open and the
  next run re-covers it.

Machine-written — every field is read-only in the UI. The engine lives in
landms/malipopay_sweep.py; the Run Now / Request Stop buttons below queue and
cooperatively stop it, mirroring TCB Reconciliation Log.

The 10-minute repair job deliberately does NOT write rows here (144 runs/day
would bloat the table); it logs to Malipopay API Log, and only when it acts.
"""

import frappe
from frappe.model.document import Document
from frappe.utils import now


@frappe.whitelist(methods=["POST"])
def run(name):
	"""Queue a sweep against an existing log (form button)."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	return frappe.get_doc("Malipopay Sweep Log", name)._execute_run()


@frappe.whitelist(methods=["POST"])
def request_stop(name):
	"""Ask a running sweep to stop after the collection it is working on."""
	frappe.only_for(("System Manager", "Accounts Manager"))
	return frappe.get_doc("Malipopay Sweep Log", name)._request_stop()


def run_in_background(name):
	"""Worker entrypoint for a queued sweep."""
	from landms.malipopay_sweep import run_malipopay_sweep

	doc = frappe.get_doc("Malipopay Sweep Log", name)
	return run_malipopay_sweep(triggered_by=doc.triggered_by or "Manual", log_name=doc.name)


class MalipopaySweepLog(Document):
	def _execute_run(self):
		if self.status in ("Running", "Queued"):
			frappe.throw("This sweep is already " + self.status + ".")
		self.db_set(
			{
				"status": "Queued",
				"started_at": now(),
				"ended_at": None,
				"stop_requested": 0,
				"collections_seen": 0,
				"transactions_booked": 0,
				"amount_booked": 0,
				"orphans_adopted": 0,
				"divergences_flagged": 0,
				"summary": "",
				"error": "",
			},
			update_modified=True,
		)
		self.rows = []
		frappe.db.delete("Malipopay Sweep Log Row", {"parent": self.name})
		frappe.db.commit()
		frappe.enqueue(
			"landms.landms.doctype.malipopay_sweep_log.malipopay_sweep_log.run_in_background",
			queue="long",
			timeout=3600,
			name=self.name,
		)
		return {"ok": True, "status": "Queued", "message": "Sweep queued."}

	def _request_stop(self):
		if self.status not in ("Running", "Queued"):
			frappe.throw("Cannot stop a sweep that is " + self.status + ".")
		if self.stop_requested:
			return {"ok": True, "status": "Ignored", "message": "Stop already requested."}
		self.db_set(
			{
				"stop_requested": 1,
				"summary": "Stop requested — the sweep will stop after the current collection.",
			},
			update_modified=True,
		)
		frappe.db.commit()
		return {"ok": True, "status": "Success", "message": "Stop requested."}
