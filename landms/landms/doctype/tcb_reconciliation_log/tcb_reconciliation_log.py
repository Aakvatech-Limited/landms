import frappe
from frappe.model.document import Document
from frappe.utils import now


@frappe.whitelist()
def run(name):
	"""Module-level entry point called by frm.call({method: 'run'}).

	The JS passes name explicitly in args to avoid relying on Frappe
	automatically injecting doctype/name into the POST body.
	"""
	doc = frappe.get_doc("TCB Reconciliation Log", name)
	return doc._execute_run()


class TCBReconciliationLog(Document):
	def _execute_run(self):
		"""Trigger reconciliation from the form button.

		Sets the doc to Running, commits so the UI can show it, then calls
		run_tcb_reconciliation_job which updates this same doc with results.
		"""
		if self.status not in ("Draft", "Failed"):
			frappe.throw(f"Cannot run a reconciliation log that is already {self.status}.")

		self.db_set({
			"status": "Running",
			"started_at": now(),
			"duration_seconds": 0,
			"total_rows": 0,
			"applied": 0,
			"ignored": 0,
			"failed": 0,
			"message": "",
			"error": "",
		}, update_modified=True)
		frappe.db.commit()

		from landms.tcb import run_tcb_reconciliation_job
		run_tcb_reconciliation_job(
			start_date=str(self.date_range_start),
			end_date=str(self.date_range_end),
			triggered_by=self.triggered_by or "Manual",
			log_name=self.name,
		)
