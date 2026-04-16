import frappe
from frappe.model.document import Document
from frappe.utils import cint, now


RETRY_REGISTRATION_ENDPOINT = (
	"landms.landms.doctype.tcb_control_number.tcb_control_number."
	"TCBControlNumber.retry_registration"
)
DECLINE_CONTROL_NUMBER_ENDPOINT = (
	"landms.landms.doctype.tcb_control_number.tcb_control_number."
	"TCBControlNumber.decline_control_number"
)


# Allowed status transitions. Anything not in this map is rejected.
# Generated → Registered → Paid is the happy path. Any state can move to
# Failed (defensive), and Generated/Registered can be Declined or Expired.
ALLOWED_TRANSITIONS = {
	"Generated":  {"Registered", "Declined", "Expired", "Failed"},
	"Registered": {"Paid", "Declined", "Expired", "Failed"},
	"Paid":       set(),  # terminal
	"Declined":   set(),  # terminal
	"Expired":    set(),  # terminal
	"Failed":     {"Generated", "Registered"},  # allow recovery on retry
}


def _endpoint_result_status(result):
	status = (result or {}).get("status")
	if status in {"Success", "Failed", "Ignored", "Stopped"}:
		return status
	if cint((result or {}).get("ok")):
		mode = (result or {}).get("mode")
		return "Ignored" if mode in {"Off", "Log Only"} else "Success"
	return "Failed"


def _log_manual_action(*, event_type, endpoint, control_number, sales_order, request_payload, result=None, error=None):
	from landms.tcb import create_tcb_api_log

	response_payload = result if isinstance(result, dict) else {"message": result or ""}
	return create_tcb_api_log(
		direction="Inbound",
		event_type=event_type,
		status="Failed" if error else _endpoint_result_status(result),
		processing_mode="Form Action",
		endpoint=endpoint,
		external_reference=control_number,
		sales_order=sales_order,
		request_payload=request_payload,
		response_payload=response_payload,
		error=error or (result or {}).get("error"),
	)


class TCBControlNumber(Document):
	"""Registry row tracking the lifecycle of a single TCB control number.

	One row per generated control number. The doc name IS the control number,
	so uniqueness is enforced at the database level.

	State transitions are written via the helper methods below
	(mark_registered, mark_paid, mark_declined, mark_expired, mark_failed).
	Direct status edits from the UI are blocked because the field is read_only,
	but the validate() guard catches API misuse too.
	"""

	def before_insert(self):
		if not self.generated_at:
			self.generated_at = now()
		if not self.status:
			self.status = "Generated"

	def validate(self):
		if self.has_value_changed("status") and not self.is_new():
			previous = self.get_doc_before_save()
			old_status = previous.status if previous else None
			new_status = self.status
			if old_status and new_status and old_status != new_status:
				allowed = ALLOWED_TRANSITIONS.get(old_status, set())
				if new_status not in allowed:
					frappe.throw(
						f"Illegal TCB Control Number transition: {old_status} → {new_status}. "
						f"Allowed from {old_status}: {sorted(allowed) or 'none (terminal)'}."
					)

	# ------------------------------------------------------------------ #
	#  Lifecycle helpers                                                   #
	# ------------------------------------------------------------------ #

	def mark_registered(self, log_name=None, note=None):
		self._transition("Registered", "registered_at", log_name=log_name, note=note,
		                 event_type="Reference Create", event_status="Success")

	def mark_paid(self, payment_entry, payment_reference, paid_amount,
	              log_name=None, note=None):
		self.payment_entry = payment_entry
		self.payment_reference = payment_reference
		self.paid_amount = paid_amount
		self._transition("Paid", "paid_at", log_name=log_name, note=note,
		                 event_type="IPN Callback", event_status="Success")

	def mark_declined(self, log_name=None, note=None):
		self._transition("Declined", "declined_at", log_name=log_name, note=note,
		                 event_type="Reference Decline", event_status="Success")

	def mark_expired(self, note=None):
		self._transition("Expired", "declined_at", note=note,
		                 event_type="Expiry", event_status="Ignored")

	def mark_failed(self, note=None, log_name=None, event_type="Reference Create"):
		self._transition("Failed", None, log_name=log_name, note=note,
		                 event_type=event_type, event_status="Failed")

	# ------------------------------------------------------------------ #
	#  Manual decline (UI button)                                         #
	# ------------------------------------------------------------------ #

	@frappe.whitelist()
	def decline_control_number(self):
		"""Manually decline this control number via the TCB Reference Decline URL.

		Called from the form button. Works for Generated, Registered and Failed
		statuses. Terminal statuses (Paid, Declined, Expired) are rejected.
		"""
		request_payload = {
			"action": "decline_control_number",
			"control_number": self.name,
			"sales_order": self.sales_order,
			"status": self.status,
			"user": frappe.session.user,
		}
		TERMINAL = ("Paid", "Declined", "Expired")
		if self.status in TERMINAL:
			message = f"Control number {self.name} has status {self.status} and cannot be declined."
			_log_manual_action(
				event_type="Reference Decline",
				endpoint=DECLINE_CONTROL_NUMBER_ENDPOINT,
				control_number=self.name,
				sales_order=self.sales_order,
				request_payload=request_payload,
				error=message,
			)
			frappe.throw(message)

		from landms.tcb import decline_reference_for_sales_order
		try:
			result = decline_reference_for_sales_order(
				self.sales_order or "",
				self.name,
			)
		except Exception:
			_log_manual_action(
				event_type="Reference Decline",
				endpoint=DECLINE_CONTROL_NUMBER_ENDPOINT,
				control_number=self.name,
				sales_order=self.sales_order,
				request_payload=request_payload,
				error=frappe.get_traceback(),
			)
			raise
		_log_manual_action(
			event_type="Reference Decline",
			endpoint=DECLINE_CONTROL_NUMBER_ENDPOINT,
			control_number=self.name,
			sales_order=self.sales_order,
			request_payload=request_payload,
			result=result,
		)
		return result

	# ------------------------------------------------------------------ #
	#  Manual retry registration (UI button)                               #
	# ------------------------------------------------------------------ #

	@frappe.whitelist()
	def retry_registration(self):
		"""Retry TCB reference registration for control numbers stuck at
		Generated or Failed.

		Called from the form button. Calls the same
		register_reference_for_sales_order used during SO submission.
		"""
		request_payload = {
			"action": "retry_registration",
			"control_number": self.name,
			"sales_order": self.sales_order,
			"status": self.status,
			"user": frappe.session.user,
		}
		RETRYABLE = ("Generated", "Failed")
		if self.status not in RETRYABLE:
			message = (
				f"Control number {self.name} has status {self.status} and cannot be retried. "
				f"Only Generated or Failed control numbers can be retried."
			)
			_log_manual_action(
				event_type="Reference Create",
				endpoint=RETRY_REGISTRATION_ENDPOINT,
				control_number=self.name,
				sales_order=self.sales_order,
				request_payload=request_payload,
				error=message,
			)
			frappe.throw(message)

		if not self.sales_order:
			message = (
				f"Control number {self.name} has no linked Sales Order. Cannot retry registration."
			)
			_log_manual_action(
				event_type="Reference Create",
				endpoint=RETRY_REGISTRATION_ENDPOINT,
				control_number=self.name,
				sales_order=self.sales_order,
				request_payload=request_payload,
				error=message,
			)
			frappe.throw(message)

		from landms.tcb import register_reference_for_sales_order
		try:
			result = register_reference_for_sales_order(
				self.sales_order,
				self.name,
			)
		except Exception:
			_log_manual_action(
				event_type="Reference Create",
				endpoint=RETRY_REGISTRATION_ENDPOINT,
				control_number=self.name,
				sales_order=self.sales_order,
				request_payload=request_payload,
				error=frappe.get_traceback(),
			)
			raise
		_log_manual_action(
			event_type="Reference Create",
			endpoint=RETRY_REGISTRATION_ENDPOINT,
			control_number=self.name,
			sales_order=self.sales_order,
			request_payload=request_payload,
			result=result,
		)
		return result

	def append_log(self, log_name, event_type, event_status, note=None):
		"""Append a log row to the trail without changing status."""
		self.append("tcb_api_logs", {
			"event_at":   now(),
			"event_type": event_type,
			"status":     event_status,
			"log":        log_name,
			"note":       (note or "")[:500],
		})
		self.save(ignore_permissions=True)

	# ------------------------------------------------------------------ #
	#  Internal                                                            #
	# ------------------------------------------------------------------ #

	def _transition(self, new_status, timestamp_field, *,
	                log_name=None, note=None, event_type=None, event_status=None):
		self.status = new_status
		if timestamp_field:
			self.set(timestamp_field, now())
		self.last_event = (note or f"{event_type or new_status} → {new_status}")[:500]
		self.append("tcb_api_logs", {
			"event_at":   now(),
			"event_type": event_type or new_status,
			"status":     event_status or new_status,
			"log":        log_name,
			"note":       (note or "")[:500],
		})
		self.save(ignore_permissions=True)
