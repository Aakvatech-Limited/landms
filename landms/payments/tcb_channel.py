"""TCB payment channel — a PURE FORWARDER.

Every method here returns the result of the existing ``landms.tcb`` /
``landms.api.tcb`` function, unchanged. NO logic lives in this file. This is the
iron rule of the strangler-fig rollout: until the abstraction is proven, the TCB
path must be byte-identical to today's behaviour, so the adapter forwards and
nothing more.

Consequences of "pure forwarder":
  * commit-on-success, gate placement (before_submit / before_cancel because
    create_tcb_api_log commits mid-txn), the MySQL named lock, and the
    Guest -> Administrator elevation all stay exactly where they are in
    ``landms.tcb`` — this file never reimplements them.
  * if a forwarded function's behaviour changes, the channel changes with it,
    for free.
"""

from __future__ import annotations

from typing import Any

from landms.payments.base import ChannelResult, PaymentChannel
from landms.payments.registry import register_channel


class TCBChannel(PaymentChannel):
	key = "TCB"
	label = "TCB (Tanzania Commercial Bank)"

	# ---- outbound: reference lifecycle ---------------------------------- #

	def generate_reference(
		self, sales_order_name: str, *, related: bool = False, pattern: str | None = None
	) -> str:
		from landms.tcb import generate_control_number

		return generate_control_number(sales_order_name, related=related, pattern=pattern)

	def is_valid_reference(
		self, value: str, *, sales_order_name: str | None = None, related: bool = False
	) -> bool:
		from landms.tcb import is_valid_control_number

		return is_valid_control_number(value, sales_order_name=sales_order_name, related=related)

	def register_reference(self, sales_order_name: str, reference: str) -> ChannelResult:
		from landms.tcb import register_reference_for_sales_order

		result = register_reference_for_sales_order(sales_order_name, reference)
		# result is already the {"ok", "message", ...} dict TCB returns; return
		# it verbatim so existing callers see no change.
		return result

	def decline_reference(self, sales_order_name: str, reference: str) -> ChannelResult:
		from landms.tcb import decline_reference_for_sales_order

		return decline_reference_for_sales_order(sales_order_name, reference)

	def never_registered(self, reference: str) -> bool:
		from landms.tcb import never_registered

		return never_registered(reference)

	def payment_ever_arrived(self, reference: str) -> bool:
		from landms.tcb import payment_ever_arrived

		return payment_ever_arrived(reference)

	# ---- inbound: callback handling ------------------------------------- #
	#
	# TCB's inbound path is the whitelisted endpoint landms.api.tcb.ipn_callback,
	# which reads the raw request, authenticates it, normalizes it, and hands it
	# to the shared core. It is intentionally NOT split into parse/verify here:
	# the endpoint stays the single source of truth until Malipopay needs a
	# second endpoint. These forwarders exist for symmetry / future shared
	# routing and are not on the live path today.

	def parse_inbound(self, request: Any) -> dict[str, Any]:
		raise NotImplementedError(
			"TCB inbound is handled end-to-end by landms.api.tcb.ipn_callback; "
			"it is not split into parse/verify on the live path."
		)

	def verify_inbound(self, request: Any) -> ChannelResult:
		raise NotImplementedError("TCB inbound auth is handled inside landms.api.tcb.ipn_callback.")

	# ---- ownership / routing / config ----------------------------------- #

	def owns_reference(self, reference: str) -> bool:
		"""A reference is TCB's if it exists in the TCB Control Number registry."""
		import frappe

		if not reference:
			return False
		return bool(frappe.db.exists("TCB Control Number", reference))

	def is_enabled(self, land_acquisition: str | None) -> bool:
		"""TCB is enabled for a project when it is the project's selected channel.

		Pre-field (Stage 2 not yet applied) this is always True, because the
		registry default is TCB.
		"""
		from landms.payments.registry import channel_for_land_acquisition

		return channel_for_land_acquisition(land_acquisition).key == self.key

	def run_reconciliation(
		self, start_date: str | None = None, end_date: str | None = None
	) -> dict[str, Any]:
		from landms.tcb import run_tcb_reconciliation_job

		return run_tcb_reconciliation_job(start_date=start_date, end_date=end_date)


# Register on import (landms.payments.__init__ imports this module).
register_channel(TCBChannel())
