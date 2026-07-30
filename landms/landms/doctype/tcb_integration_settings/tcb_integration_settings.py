from urllib.parse import urlparse

import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt, get_url



IPN_CALLBACK_PATH = "/api/method/landms.api.tcb.ipn_callback"


class TCBIntegrationSettings(Document):

	def onload(self):
		"""Populate the read-only IPN URL every time the form opens.

		The value is not persisted — it's derived from the site's public URL
		so that moving between sites (dev / staging / prod) always shows
		the right endpoint to hand to TCB.
		"""
		try:
			self.ipn_callback_url = get_url(IPN_CALLBACK_PATH)
		except Exception:
			# Never let a display helper break the form.
			self.ipn_callback_url = IPN_CALLBACK_PATH

	def validate(self):
		self._validate_live_credentials()
		self._validate_inbound_consistency()
		self._validate_reconciliation_consistency()
		self._validate_timeouts()
		self._validate_ipn_auth()
		# Always clear the persisted value — it's a runtime display only.
		self.ipn_callback_url = ""

	def _validate_ipn_auth(self):
		"""Enforce requires a token. Selecting Enforce without a configured token would make
		the inbound check fail closed at payment time (rejecting every callback) — catch it
		here at save time instead, with a clear message, so the operator sets the secret first.
		"""
		if (self.ipn_auth_mode or "Off") != "Enforce":
			return
		token = ""
		try:
			token = (self.get_password("ipn_auth_token", raise_exception=False) or "").strip()
		except Exception:
			token = (self.ipn_auth_token or "").strip()
		if not token:
			frappe.throw(
				"IPN Auth Enforcement is set to Enforce, but no IPN Auth Token is configured. "
				"Generate a token, paste it here, and give the same value to TCB before selecting "
				"Enforce — otherwise every payment callback would be rejected. "
				"Use Log Only until TCB is confirmed to be sending the token."
			)

	# ------------------------------------------------------------------ #
	#  Credentials                                                         #
	# ------------------------------------------------------------------ #

	def _validate_live_credentials(self):
		"""When live calls are about to happen, we need real credentials and URLs."""
		# Light URL shape check even when disabled — catches typos early.
		for fieldname in ("reference_create_url", "reference_decline_url", "reconciliation_url"):
			url = (getattr(self, fieldname, None) or "").strip()
			if url and not _looks_like_https_url(url):
				frappe.throw(
					f"{self.meta.get_label(fieldname)} must be a full https:// URL."
				)
			# Normalize whitespace.
			setattr(self, fieldname, url)

		if not cint(self.enabled):
			return

		needs_outbound = self.outbound_mode == "Live"
		needs_inbound = self.inbound_mode == "Apply Payment"
		if not (needs_outbound or needs_inbound):
			return

		missing = []

		if needs_outbound:
			if not (self.reference_create_url or "").strip():
				missing.append("Reference Create URL")
			if not (self.reference_decline_url or "").strip():
				missing.append("Reference Decline URL")

		if missing:
			frappe.throw(
				"TCB integration fields are required when Outbound Mode is Live or Inbound Mode "
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


def _looks_like_https_url(value: str) -> bool:
	try:
		parsed = urlparse(value)
	except Exception:
		return False
	return parsed.scheme in ("https", "http") and bool(parsed.netloc)
