"""The PaymentChannel contract.

Every payment channel (TCB, Malipopay, future) implements exactly the verbs
below. Nothing here is open-ended — each verb corresponds to something the TCB
channel already does today; the abstraction only gives those actions a stable,
channel-neutral shape.

What is channel-SPECIFIC (lives in the adapter):
  * outbound register / decline of a reference,
  * minting / validating the channel's reference (control number),
  * parsing + authenticating the channel's inbound callback envelope,
  * the channel's own reconciliation pull,
  * the per-project enable check and reference-ownership lookup.

What is SHARED CORE (stays in payment_sync.py, written once, channel-neutral)
and is deliberately NOT part of this contract:
  * creating the Payment Entry, the government-share JE, contract sync,
  * the Guest -> Administrator elevation on the IPN path,
  * the (transaction_id, reference) idempotency dedupe + named lock +
    commit-before-release.

So an adapter's job ends at ``parse_inbound`` / ``verify_inbound``; everything
after a normalized payment is handed to the shared core.
"""

from __future__ import annotations

from typing import Any


class ChannelResult(dict):
	"""A small ``{"ok": bool, "message": str, ...}`` result.

	Channels already return plain dicts of this shape today (e.g. TCB's
	``register_reference_for_sales_order``). ``ChannelResult`` is just a typed
	convenience constructor — it IS a dict, so existing callers that read
	``result["ok"]`` / ``result.get("message")`` keep working unchanged.
	"""

	def __init__(self, ok: bool, message: str = "", **extra: Any) -> None:
		super().__init__(ok=bool(ok), message=message or "", **extra)


class PaymentChannel:
	"""Base class for a payment channel adapter.

	Subclasses set a unique ``key`` (the stable identifier stored on Land
	Acquisition and stamped on each reference) and a human ``label``, then
	implement the verbs below. The base raises ``NotImplementedError`` for each
	so a half-built channel fails loudly rather than silently doing nothing.
	"""

	#: Stable identifier, e.g. "TCB" / "Malipopay". Stored in data and stamped
	#: on every reference this channel mints. NEVER change an existing key —
	#: old references carry it forever.
	key: str = ""

	#: Human-readable name for UI / logs.
	label: str = ""

	# ---- outbound: reference lifecycle ---------------------------------- #

	def generate_reference(
		self, sales_order_name: str, *, related: bool = False, pattern: str | None = None
	) -> str:
		"""Mint a new reference (control number) for a Sales Order. ``pattern``
		is the project's numbering pattern when the caller already has it;
		None means the adapter derives it itself."""
		raise NotImplementedError

	def is_valid_reference(
		self, value: str, *, sales_order_name: str | None = None, related: bool = False
	) -> bool:
		"""Strict format check for one of this channel's references."""
		raise NotImplementedError

	def register_reference(self, sales_order_name: str, reference: str) -> ChannelResult:
		"""Register a reference with the channel (the SO-submit gate)."""
		raise NotImplementedError

	def decline_reference(self, sales_order_name: str, reference: str) -> ChannelResult:
		"""Decline / release a reference (the SO-cancel/close gate)."""
		raise NotImplementedError

	# ---- inbound: callback handling ------------------------------------- #

	def parse_inbound(self, request: Any) -> dict[str, Any]:
		"""Normalize a raw inbound callback into the shared shape:
		``{"reference", "amount", "payment_date", "transaction_id"}``.
		"""
		raise NotImplementedError

	def verify_inbound(self, request: Any) -> ChannelResult:
		"""Authenticate a raw inbound callback (token / IP / signature)."""
		raise NotImplementedError

	# ---- ownership / routing / config ----------------------------------- #

	def owns_reference(self, reference: str) -> bool:
		"""True if this channel minted / owns ``reference`` (registry lookup)."""
		raise NotImplementedError

	def is_enabled(self, land_acquisition: str | None) -> bool:
		"""True if this channel is the selected channel for ``land_acquisition``."""
		raise NotImplementedError

	def run_reconciliation(
		self, start_date: str | None = None, end_date: str | None = None
	) -> dict[str, Any]:
		"""Run this channel's reconciliation pull for a date range."""
		raise NotImplementedError

	# ---- niceties -------------------------------------------------------- #

	def __repr__(self) -> str:  # pragma: no cover - debug aid
		return f"<PaymentChannel {self.key or self.__class__.__name__}>"
