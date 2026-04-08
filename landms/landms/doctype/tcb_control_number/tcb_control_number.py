import frappe
from frappe.model.document import Document
from frappe.utils import now


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
