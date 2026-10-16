import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import get_url

WEBHOOK_CALLBACK_PATH = "/api/method/landms.api.malipopay.webhook"


class MalipopaySettings(Document):
	"""Global settings for the Malipopay payment channel.

	Deliberately minimal: connection + secrets + collection defaults. Whether a
	given PROJECT collects via Malipopay (and whether it issues the numbers or
	depends on another channel) lives on the Land Acquisition's Payment
	Channels tab — never here. This single only says HOW to talk to Malipopay,
	not WHO uses it.
	"""

	def onload(self):
		"""Populate the read-only callback URL every time the form opens — derived
		from the site's public URL (dev / staging / prod each show their own), never persisted."""
		try:
			self.webhook_callback_url = get_url(WEBHOOK_CALLBACK_PATH)
		except Exception:
			self.webhook_callback_url = WEBHOOK_CALLBACK_PATH

	def validate(self):
		self.webhook_callback_url = ""  # runtime display only
		if self.enabled:
			if not (self.base_url or "").strip():
				frappe.throw(_("Base URL is required while Malipopay is enabled."))
			if not self.get_password("api_token", raise_exception=False):
				frappe.throw(_("API Token is required while Malipopay is enabled."))
			# The secret only exists after the one-time webhook registration, which
			# happens AFTER outbound goes live (bills must be creatable first). So it
			# gates only the mode that books money; earlier modes merely warn — the
			# receiver files unverifiable deliveries as Held, never processes them.
			if not self.get_password("webhook_signing_secret", raise_exception=False):
				if (self.inbound_mode or "Off") == "Apply Payment":
					frappe.throw(
						_(
							"Webhook Signing Secret is required before Inbound Mode can be "
							"'Apply Payment' — register the webhook first (button below)."
						)
					)
				frappe.msgprint(
					_(
						"Webhook Signing Secret is empty: inbound deliveries will be filed as "
						"Held (unverifiable) until the webhook is registered."
					),
					indicator="orange",
					alert=True,
				)
