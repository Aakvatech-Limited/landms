# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Malipopay Collection — one row per collection EPOCH at Malipopay.

The registry mirror of a bill raised at Malipopay against OUR control number
(always TCB-issued; Malipopay is a dependent collection rail, never an issuer).

Design rules this doctype enforces (see Desktop/MALIPOPAY-ARCHITECTURE.md):

- EPOCHS, not updates: Malipopay's create endpoint is not idempotent and their
  CANCELLED is one-way, so a reopened forfeiture gets a brand-new row. At most
  ONE non-Dead row may exist per control number (validated here).
- State transitions are guarded exactly like TCB Control Number's
  ALLOWED_TRANSITIONS — machine-written only, no illegal jumps.
- The provider snapshot fields are display-only. Closure decisions are made
  from breakdown summary.fullyPaid / outstanding and from OUR ledger — never
  from a mirrored status.
"""

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, now

# Our lifecycle for one epoch. "Dead" is terminal — a reopen mints a NEW row.
# "Creating" is the write-ahead intent around the non-idempotent create call:
# a crash mid-HTTP leaves a Creating row as evidence, and the repair job
# recovers by querying Malipopay by customerReference (adopt-don't-create).
# "Suspended" mirrors their SUSPENDED (contract v1.2 §12.2): a CLOSED order's
# bill is paused, keeping its money, until the order is reopened (resume) or
# closed for good (expire). The two "… Requested" states are the intent
# written inside the caller's transaction; a worker makes the call after commit.
ALLOWED_TRANSITIONS = {
	"Pending Create": {"Creating", "Active", "Create Failed", "Kill Requested", "Dead"},
	"Creating": {"Active", "Create Failed", "Kill Requested", "Dead"},
	"Active": {"Suspend Requested", "Kill Requested", "Dead", "Needs Attention"},
	"Create Failed": {"Pending Create", "Creating", "Active", "Kill Requested", "Dead"},
	"Suspend Requested": {"Suspended", "Resume Requested", "Kill Requested", "Dead", "Needs Attention"},
	"Suspended": {"Resume Requested", "Kill Requested", "Dead"},
	"Resume Requested": {
		"Active",
		"Suspended",
		"Suspend Requested",
		"Kill Requested",
		"Dead",
		"Needs Attention",
	},
	"Kill Requested": {"Dead", "Active", "Needs Attention"},
	"Needs Attention": {"Active", "Suspend Requested", "Kill Requested", "Dead"},
	"Dead": set(),
}


class MalipopayCollection(Document):
	def before_insert(self):
		if not self.state:
			self.state = "Pending Create"

	def validate(self):
		self._validate_state_transition()
		self._validate_single_live_epoch()

	def _validate_state_transition(self):
		"""Block illegal state jumps on existing docs (API misuse guard —
		the field is already read_only in the UI)."""
		if self.is_new():
			return
		before = self.get_doc_before_save()
		if not before or before.state == self.state:
			return
		allowed = ALLOWED_TRANSITIONS.get(before.state, set())
		if self.state not in allowed:
			frappe.throw(
				_("Illegal Malipopay Collection transition: {0} → {1}. Allowed from {0}: {2}.").format(
					before.state, self.state, sorted(allowed) or _("none (terminal)")
				)
			)

	def _validate_single_live_epoch(self):
		"""At most one non-Dead epoch per control number. Two live bills for
		one reference means a customer can pay both — the exact ambiguity the
		integration contract warns about."""
		if not self.control_number or self.state == "Dead":
			return
		other = frappe.db.get_value(
			"Malipopay Collection",
			{
				"control_number": self.control_number,
				"state": ["!=", "Dead"],
				"name": ["!=", self.name or ""],
			},
			"name",
		)
		if other:
			frappe.throw(
				_(
					"Control number {0} already has a live Malipopay Collection ({1}). "
					"Mark it Dead before opening a new epoch."
				).format(self.control_number, other)
			)

	# ------------------------------------------------------------------
	# Lifecycle helpers — the only sanctioned way later stages move state.
	# Every transition runs through validate()'s guard via save().
	# ------------------------------------------------------------------

	def set_state(self, new_state: str, note: str | None = None):
		self.state = new_state
		if note:
			self.last_event = note[:500]
		if new_state == "Active" and not self.activated_at:
			self.activated_at = now()
		if new_state == "Dead":
			self.dead_at = now()
		self.save(ignore_permissions=True)

	def flag_attention(self, reason: str):
		"""Raise the operator flag without changing lifecycle state."""
		self.db_set("needs_attention", 1, update_modified=False)
		self.db_set("attention_reason", (reason or "")[:500], update_modified=False)

	@frappe.whitelist(methods=["POST"])  # changes data / calls Malipopay or TCB
	def retry_registration(self):
		"""Form button: register this payment number with Malipopay again.

		Runs the same create-or-adopt path as the Sales Order, which always
		asks Malipopay first: an existing payable bill is adopted, a bill is
		created only when Malipopay clearly answers that none exists, and an
		answer without a Malipopay reference counts as a failure."""
		frappe.only_for(("System Manager", "Accounts Manager"))
		if self.state not in RETRYABLE_STATES:
			return {
				"ok": False,
				"message": _("This bill is {0}; only {1} bills can be retried.").format(
					self.state, ", ".join(RETRYABLE_STATES)
				),
			}
		if not self.sales_order or not self.control_number:
			return {"ok": False, "message": _("This bill has no Sales Order or control number to register.")}

		from landms.malipopay import create_or_adopt_collection

		state_before = self.state
		result = create_or_adopt_collection(self.name)
		ml = result.get("ml_reference")
		if result.get("status") == "Adopted":
			message = _("Malipopay already had bill {0} for {1}; it has been linked.").format(
				ml, self.control_number
			)
		elif result.get("status") == "Created":
			message = _("Registered with Malipopay: bill {0} for {1}.").format(ml, self.control_number)
		else:
			message = result.get("message") or _("Registration failed. Check Malipopay API Log for details.")
		out = {
			"ok": bool(result.get("ok")),
			"status": result.get("status"),
			"ml_reference": ml,
			"message": message,
		}
		self._log_form_action("retry_registration", "Collection Create", state_before, out)
		return out

	@frappe.whitelist(methods=["POST"])  # changes data / calls Malipopay or TCB
	def expire_at_malipopay(self):
		"""Form button — the Malipopay counterpart of TCB's "Decline at TCB":
		close this bill at Malipopay so it can no longer take money.

		Only this bill is closed. The TCB number is not touched: TCB issues the
		numbers and leads, so declining the number everywhere is done with the
		Decline button on the TCB Control Number (Malipopay then follows).
		If Malipopay ever issues a project's numbers (blocked today by Land
		Acquisition._validate_issuer_can_mint), this button becomes the decline
		gate and must go through payments.router so TCB follows it."""
		frappe.only_for(("System Manager", "Accounts Manager"))
		state_before = self.state
		refusal = None
		if self.state in ("Suspend Requested", "Suspended", "Resume Requested"):
			# a closed order's paused bill ends only through the order, so the
			# order is marked and can no longer be reopened
			refusal = _(
				"This bill belongs to closed order {0}. End it with Close for good at Malipopay "
				"on the Sales Order (System Manager), or reopen the order."
			).format(self.sales_order)
		elif self.state not in EXPIRABLE_STATES:
			refusal = _("This bill is {0}; only {1} bills can be expired.").format(
				self.state, ", ".join(EXPIRABLE_STATES)
			)
		elif cint(self.provider_fully_paid):
			refusal = _("This bill is paid in full at Malipopay and cannot be expired.")
		if refusal:
			out = {"ok": False, "message": refusal}
			self._log_form_action("expire_at_malipopay", "Expire", state_before, out)
			return out

		from landms.malipopay import expire_collection, request_kill_for_collection

		if self.state != "Kill Requested":
			request_kill_for_collection(
				self, _("Expired from the form by {0}").format(frappe.session.user), enqueue=False
			)
			frappe.db.commit()
		if self.state == "Dead":
			result = frappe._dict(ok=True, message=_("no bill was ever sent to Malipopay"))
		else:
			result = expire_collection(self.name)
		ml = frappe.db.get_value("Malipopay Collection", self.name, "ml_reference") or self.control_number
		if result.get("ok"):
			message = _("Bill {0} is closed at Malipopay ({1}).").format(ml, result.get("message"))
		else:
			message = _(
				"Malipopay did not expire bill {0}: {1}. It stays Kill Requested and is retried every hour."
			).format(ml, result.get("message"))
		out = {"ok": bool(result.get("ok")), "message": message}
		self._log_form_action("expire_at_malipopay", "Expire", state_before, out)
		return out

	@frappe.whitelist(methods=["POST"])  # changes data / calls Malipopay or TCB
	def check_payments(self):
		"""Form button: read this bill's receipts from Malipopay now and book any
		payment LandMS has not recorded — the daily sweep for one bill, for when
		a customer has paid and the webhook did not arrive. Safe to repeat: a
		receipt is never booked twice."""
		frappe.only_for(("System Manager", "Accounts Manager"))
		if not self.ml_reference:
			out = {"ok": False, "message": _("This bill has no Malipopay reference yet — nothing to check.")}
			self._log_form_action("check_payments", "Breakdown", self.state, out)
			return out

		from landms.malipopay import book_receipts_for_collection

		result = book_receipts_for_collection(self.name)
		if result.get("ok"):
			if result.booked:
				message = _("Booked {0} new payment(s): {1}.").format(
					len(result.booked), ", ".join(result.booked)
				)
			else:
				message = _("No new payments at Malipopay — everything is already booked.")
		else:
			message = result.get("message") or _(
				"Could not read the bill from Malipopay. See Malipopay API Log."
			)
		out = {"ok": bool(result.get("ok")), "booked": result.get("booked") or [], "message": message}
		self._log_form_action("check_payments", "Breakdown", self.state, out)
		return out

	@frappe.whitelist(methods=["POST"])  # changes data / calls Malipopay or TCB
	def report_to_malipopay(self):
		"""Form button: tell Malipopay now about this order's payments made
		outside it (TCB, counter) — the 05:10 daily pass for one bill, and the
		retry for a message Malipopay refused or never answered. Safe to repeat:
		the pass sends only what Malipopay does not already have."""
		frappe.only_for(("System Manager", "Accounts Manager"))
		if not self.ml_reference:
			out = {"ok": False, "message": _("This bill has no Malipopay reference yet — nothing to report.")}
			self._log_form_action("report_to_malipopay", "External Receipt", self.state, out)
			return out

		from landms.malipopay_external import sync_external_entries

		state_before = self.state
		result = sync_external_entries(self.name, _("button by {0}").format(frappe.session.user))
		if result.message == "in step":
			out = {
				"ok": True,
				"message": _("Malipopay already has every payment made outside it — nothing to send."),
			}
		elif result.sent:
			lines, ok = [], True
			for row_name, status in result.sent:
				row = frappe.db.get_value(
					"Malipopay External Entry",
					row_name,
					["entry_type", "amount", "payment_entry", "provider_code", "message"],
					as_dict=True,
				)
				line = _("{0} {1} for payment {2}: {3}").format(
					row.entry_type, row.amount, row.payment_entry, status
				)
				if status not in ("Sent", "Replayed"):
					ok = False
					line += " — " + _("Malipopay said: {0} {1}").format(
						row.provider_code or "", row.message or ""
					)
				lines.append(line)
			out = {"ok": ok, "message": "<br>".join(lines)}
		else:
			out = {"ok": False, "message": _("Nothing was sent to Malipopay: {0}").format(result.message)}
		self._log_form_action("report_to_malipopay", "External Receipt", state_before, out)
		return out

	def _log_form_action(self, action, event_type, state_before, result):
		"""Who pressed which button, and what came back — the TCB form's audit
		trail (processing mode "Form Action")."""
		from landms.malipopay import create_malipopay_api_log

		create_malipopay_api_log(
			direction="Inbound",
			event_type=event_type,
			status="Success" if result.get("ok") else "Failed",
			processing_mode="Form Action",
			endpoint="landms.landms.doctype.malipopay_collection.malipopay_collection.MalipopayCollection."
			+ action,
			control_number=self.control_number,
			ml_reference=self.ml_reference,
			malipopay_collection=self.name,
			sales_order=self.sales_order,
			request_payload={"action": action, "user": frappe.session.user, "state": state_before},
			response_payload=result,
		)
		frappe.db.commit()


# States a person may retry from the form. "Creating" is included for a call
# that died mid-flight; the retry takes the same lock and asks Malipopay
# before creating, so it cannot race a live attempt into a duplicate bill.
RETRYABLE_STATES = ("Create Failed", "Pending Create", "Creating")

# States a person may expire from the form. Create Failed / Pending Create are
# included because a create that timed out may still have made a bill: the
# expire asks Malipopay first, and a bill that never existed simply goes Dead.
EXPIRABLE_STATES = ("Active", "Needs Attention", "Kill Requested", "Create Failed", "Pending Create")
