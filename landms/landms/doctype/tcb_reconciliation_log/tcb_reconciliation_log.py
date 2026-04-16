import frappe
from frappe.model.document import Document
from frappe.utils import now


RUN_ENDPOINT = "/api/method/landms.landms.doctype.tcb_reconciliation_log.tcb_reconciliation_log.run"
REQUEST_STOP_ENDPOINT = "/api/method/landms.landms.doctype.tcb_reconciliation_log.tcb_reconciliation_log.request_stop"


def _log_reconciliation_endpoint_failure(*, endpoint: str, action: str, name: str, error: str) -> None:
	from landms.tcb import create_tcb_api_log

	create_tcb_api_log(
		direction="Inbound",
		event_type="Reconciliation",
		status="Failed",
		processing_mode="Endpoint",
		endpoint=endpoint,
		reconciliation_log=name,
		request_payload={
			"action": action,
			"name": name,
			"user": frappe.session.user,
		},
		response_payload={"message": f"Reconciliation {action} request failed."},
		error=error,
	)


@frappe.whitelist()
def run(name):
	"""Module-level entry point called by frm.call({method: 'run'}).

	The JS passes name explicitly in args to avoid relying on Frappe
	automatically injecting doctype/name into the POST body.
	"""
	try:
		doc = frappe.get_doc("TCB Reconciliation Log", name)
		return doc._execute_run()
	except Exception:
		_log_reconciliation_endpoint_failure(
			endpoint=RUN_ENDPOINT,
			action="run",
			name=name,
			error=frappe.get_traceback(),
		)
		raise


@frappe.whitelist()
def request_stop(name):
	"""Ask a running reconciliation job to stop after its current unit of work."""
	try:
		doc = frappe.get_doc("TCB Reconciliation Log", name)
		return doc._request_stop()
	except Exception:
		_log_reconciliation_endpoint_failure(
			endpoint=REQUEST_STOP_ENDPOINT,
			action="request_stop",
			name=name,
			error=frappe.get_traceback(),
		)
		raise


def run_in_background(name):
	"""Worker entrypoint for a queued reconciliation run."""
	doc = frappe.get_doc("TCB Reconciliation Log", name)
	from landms.tcb import create_tcb_api_log, run_tcb_reconciliation_job

	create_tcb_api_log(
		direction="Inbound",
		event_type="Reconciliation",
		status="Success",
		processing_mode="Background Job",
		endpoint="landms.landms.doctype.tcb_reconciliation_log.tcb_reconciliation_log.run_in_background",
		reconciliation_log=doc.name,
		request_payload={
			"action": "run_in_background",
			"name": doc.name,
			"date_range_start": str(doc.date_range_start),
			"date_range_end": str(doc.date_range_end),
			"triggered_by": doc.triggered_by or "Manual",
		},
		response_payload={"message": "Queued reconciliation job started."},
	)
	return run_tcb_reconciliation_job(
		start_date=str(doc.date_range_start),
		end_date=str(doc.date_range_end),
		triggered_by=doc.triggered_by or "Manual",
		log_name=doc.name,
	)


class TCBReconciliationLog(Document):
	def _execute_run(self):
		"""Trigger reconciliation from the form button.

		Sets the doc to Running, commits so the UI can show it, then queues
		the worker job which updates this same doc with results.
		"""
		if self.status not in ("Draft", "Failed", "Stopped"):
			frappe.throw(f"Cannot run a reconciliation log that is already {self.status}.")

		self.db_set({
				"status": "Running",
				"started_at": now(),
				"completed_at": None,
				"duration_seconds": 0,
				"total_rows": 0,
				"applied": 0,
				"ignored": 0,
				"failed": 0,
				"message": "",
				"error": "",
				"stop_requested": 0,
				"stop_requested_at": None,
				"stop_requested_by": "",
			}, update_modified=True)
		frappe.db.commit()

		from landms.tcb import create_tcb_api_log

		create_tcb_api_log(
			direction="Inbound",
			event_type="Reconciliation",
			status="Success",
			processing_mode="Queued",
			endpoint=RUN_ENDPOINT,
			reconciliation_log=self.name,
			request_payload={
				"action": "run",
				"name": self.name,
				"date_range_start": str(self.date_range_start),
				"date_range_end": str(self.date_range_end),
				"triggered_by": self.triggered_by or "Manual",
			},
			response_payload={"message": "Reconciliation job queued."},
		)
		frappe.enqueue(
			"landms.landms.doctype.tcb_reconciliation_log.tcb_reconciliation_log.run_in_background",
			queue="long",
			timeout=1800,
			name=self.name,
		)
		return {"ok": True, "status": "Queued", "message": "Reconciliation queued."}

	def _request_stop(self):
		if self.status != "Running":
			frappe.throw(f"Cannot stop a reconciliation log that is {self.status}.")
		if self.stop_requested:
			return {"ok": True, "status": "Ignored", "message": "Stop already requested."}

		self.db_set({
			"stop_requested": 1,
			"stop_requested_at": now(),
			"stop_requested_by": frappe.session.user,
			"message": "Stop requested. The job will stop after the current step finishes.",
		}, update_modified=True)
		frappe.db.commit()

		from landms.tcb import create_tcb_api_log

		create_tcb_api_log(
			direction="Inbound",
			event_type="Reconciliation",
			status="Success",
			processing_mode="Stop Requested",
			endpoint=REQUEST_STOP_ENDPOINT,
			reconciliation_log=self.name,
			request_payload={
				"action": "request_stop",
				"name": self.name,
				"requested_by": frappe.session.user,
			},
			response_payload={"message": "Stop requested for reconciliation job."},
		)
		return {"ok": True, "status": "Success", "message": "Stop requested."}
