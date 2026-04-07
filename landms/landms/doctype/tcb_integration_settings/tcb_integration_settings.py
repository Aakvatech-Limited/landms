import re

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt


# Bounds for the control number pattern. Anything outside this is almost
# certainly a typo, not a real TCB requirement.
PATTERN_MIN_LENGTH = 5
PATTERN_MAX_LENGTH = 20
PATTERN_ALLOWED_CHARS = re.compile(r"^[0-9#]+$")


class TCBIntegrationSettings(Document):

	def validate(self):
		self._validate_control_number_pattern()
		self._validate_live_credentials()
		self._validate_inbound_consistency()
		self._validate_reconciliation_consistency()
		self._validate_timeouts()

	# ------------------------------------------------------------------ #
	#  Pattern                                                             #
	# ------------------------------------------------------------------ #

	def _validate_control_number_pattern(self):
		pattern = (self.control_number_pattern or "").strip()
		if not pattern:
			frappe.throw("Control Number Pattern is required.")

		if not PATTERN_ALLOWED_CHARS.match(pattern):
			frappe.throw(
				"Control Number Pattern may only contain digits 0-9 and the '#' character "
				"(where '#' represents a randomly generated digit)."
			)

		if len(pattern) < PATTERN_MIN_LENGTH or len(pattern) > PATTERN_MAX_LENGTH:
			frappe.throw(
				f"Control Number Pattern length must be between {PATTERN_MIN_LENGTH} "
				f"and {PATTERN_MAX_LENGTH} characters."
			)

		if "#" not in pattern:
			frappe.throw(
				"Control Number Pattern must contain at least one '#' (random digit). "
				"Otherwise every generated number would be identical."
			)

		self.control_number_pattern = pattern

	# ------------------------------------------------------------------ #
	#  Credentials                                                         #
	# ------------------------------------------------------------------ #

	def _validate_live_credentials(self):
		"""When live calls are about to happen, we need real credentials."""
		if not cint(self.enabled):
			return

		needs_live = self.outbound_mode == "Live" or self.inbound_mode == "Apply Payment"
		if not needs_live:
			return

		missing = []
		if not self.partner_code:
			missing.append("Partner Code")
		if not self.profile_id:
			missing.append("Profile ID / Account Number")
		# api_key is a Password field — must read via get_password to detect "is set".
		if not self.get_password("api_key", raise_exception=False):
			missing.append("API Key")

		if missing:
			frappe.throw(
				"TCB credentials are required when Outbound Mode is Live or Inbound Mode "
				"is Apply Payment. Missing: " + ", ".join(missing)
			)

	# ------------------------------------------------------------------ #
	#  Inbound consistency                                                 #
	# ------------------------------------------------------------------ #

	def _validate_inbound_consistency(self):
		if cint(self.auto_apply_callback_payments) and self.inbound_mode != "Apply Payment":
			frappe.throw(
				"Auto-Apply Callback Payments can only be enabled when Inbound Mode is 'Apply Payment'."
			)

		if cint(self.enabled) and self.inbound_mode == "Apply Payment":
			if not self.get_password("callback_token", raise_exception=False):
				frappe.throw(
					"Callback Token is required when Inbound Mode is 'Apply Payment'. "
					"This token authenticates incoming IPN requests from TCB."
				)

	# ------------------------------------------------------------------ #
	#  Reconciliation consistency                                          #
	# ------------------------------------------------------------------ #

	def _validate_reconciliation_consistency(self):
		if cint(self.auto_apply_reconciliation_payments) and not cint(self.reconciliation_enabled):
			frappe.throw(
				"Auto-Apply Reconciliation Payments cannot be enabled while Reconciliation Enabled is off."
			)

		if cint(self.reconciliation_enabled) and cint(self.reconciliation_lookback_days) < 1:
			frappe.throw("Reconciliation Lookback Days must be at least 1.")

	# ------------------------------------------------------------------ #
	#  Timeouts                                                            #
	# ------------------------------------------------------------------ #

	def _validate_timeouts(self):
		if flt(self.connect_timeout_seconds) <= 0:
			frappe.throw("Connect Timeout must be greater than zero.")
		if flt(self.read_timeout_seconds) <= 0:
			frappe.throw("Read Timeout must be greater than zero.")
