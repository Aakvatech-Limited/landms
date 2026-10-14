"""Channel registry and resolvers.

One place that answers three questions:

  * ``get_channel(key)``                   — the adapter for a stable key.
  * ``channel_for_land_acquisition(la)``   — which channel a project uses
                                             (data-driven, from LA.payment_channel).
  * ``channel_for_reference(ref)``         — which channel already owns an
                                             existing reference (by ownership,
                                             never by re-running the selector).

IRON RULES this module exists to enforce:

  1. STAMP, NEVER RECOMPUTE. Once a reference is minted by a channel, every
     later step (inbound match, decline, reconcile, reopen) must resolve the
     channel from what was STAMPED on that reference — via
     ``channel_for_reference`` — not by re-reading LA.payment_channel. That lets
     a project switch channels while its old references keep talking to the
     channel that minted them.

  2. REFERENCE OWNERSHIP. Every reference is owned by exactly one channel.
     ``channel_for_reference`` returns the first channel whose ``owns_reference``
     is true, or None. An inbound callback whose reference no channel owns is
     rejected upstream (same "not in registry" guard TCB has today).

DEFAULT: ``DEFAULT_CHANNEL_KEY`` is "TCB". Any Land Acquisition with an empty /
unknown ``payment_channel`` resolves to TCB, so the abstraction can never leave
a project without a channel.
"""

from __future__ import annotations

import frappe

from landms.payments.base import PaymentChannel

#: key -> adapter instance. Populated by ``register_channel`` at import time.
_CHANNELS: dict[str, PaymentChannel] = {}

#: The channel every project falls back to. landms is live on TCB only, so an
#: unset LA.payment_channel must behave exactly like TCB.
DEFAULT_CHANNEL_KEY = "TCB"


def register_channel(channel: PaymentChannel) -> None:
	"""Register a channel adapter under its ``key`` (idempotent)."""
	if not getattr(channel, "key", ""):
		raise ValueError("PaymentChannel must define a non-empty key")
	_CHANNELS[channel.key] = channel


def all_channels() -> list[PaymentChannel]:
	return list(_CHANNELS.values())


def get_channel(key: str | None) -> PaymentChannel:
	"""Return the adapter for ``key``, falling back to the default channel.

	An unknown or empty key resolves to the default (TCB) rather than raising,
	so a bad/empty data value can never break a live payment path.
	"""
	if key and key in _CHANNELS:
		return _CHANNELS[key]
	return _CHANNELS[DEFAULT_CHANNEL_KEY]


def channel_for_land_acquisition(land_acquisition: str | None) -> PaymentChannel:
	"""The channel a project is configured to use.

	Reads ``Land Acquisition.payment_channel`` when that field exists; anything
	empty/unknown -> the default channel. Safe to call before the field is added
	(Stage 2): ``has_column`` guards it, so pre-field it always returns TCB.
	"""
	key = DEFAULT_CHANNEL_KEY
	try:
		if land_acquisition and frappe.db.has_column("Land Acquisition", "payment_channel"):
			key = (
				frappe.db.get_value("Land Acquisition", land_acquisition, "payment_channel")
				or DEFAULT_CHANNEL_KEY
			)
	except Exception:
		# Never let channel resolution throw on a live payment path.
		key = DEFAULT_CHANNEL_KEY
	return get_channel(key)


def channel_for_sales_order(sales_order_name: str | None) -> PaymentChannel:
	"""Convenience: resolve via the SO's Land Acquisition."""
	la = None
	if sales_order_name and frappe.db.exists("Sales Order", sales_order_name):
		la = frappe.db.get_value("Sales Order", sales_order_name, "land_acquisition")
	return channel_for_land_acquisition(la)


def channel_for_reference(reference: str | None) -> PaymentChannel | None:
	"""The channel that OWNS an existing reference, or None.

	Resolves by ownership (rule 1: stamp, never recompute), so inbound routing,
	decline, reconcile and reopen all follow the channel that minted the
	reference — even if the project has since switched channels. Returns None
	when no channel owns it (caller rejects, as TCB does for unknown refs).
	"""
	if not reference:
		return None
	for channel in _CHANNELS.values():
		try:
			if channel.owns_reference(reference):
				return channel
		except Exception:
			continue
	return None
