"""Routing shims for the Sales Order lifecycle.

Drop-in replacements for the TCB functions the Sales Order hooks used to import
from ``landms.tcb`` directly (``register_reference_for_sales_order`` /
``decline_reference_for_sales_order`` / ``generate_control_number``). Each one
resolves the channel that should act and calls its adapter:

  * register / mint  -> the channel the order's Land Acquisition names under
    "Control Numbers Issued By" (``LA.payment_channel``). Empty or unknown
    resolves to TCB, so no project can ever be left without an issuer.
  * decline          -> the channel that OWNS the existing reference (stamp,
    never recompute), falling back to the project's issuer if ownership is
    unknown. A number always goes back to the channel that minted it, even if
    the project has since switched issuer.

For a TCB-issued project every call lands in the exact ``landms.tcb`` function
it always did — TCBChannel is a pure forwarder — so the Land Acquisition is the
one and only place that decides which channel a project uses. There is no
separate global switch: setting a project back to TCB on its Land Acquisition
IS the rollback.
"""

from __future__ import annotations

from typing import Any


def _channel_for_sales_order(sales_order_name: str | None):
	from landms.payments.registry import channel_for_sales_order

	return channel_for_sales_order(sales_order_name)


# --------------------------------------------------------------------------- #
#  Drop-in replacements for the TCB lifecycle functions                       #
# --------------------------------------------------------------------------- #


def register_reference_for_sales_order(sales_order_name: str, control_number: str) -> dict[str, Any]:
	"""Register a payment reference for a Sales Order (SO-submit gate) with the
	project's issuing channel."""
	return _channel_for_sales_order(sales_order_name).register_reference(sales_order_name, control_number)


#: Rails that can take payment on a number the issuer could not register at
#: submit: channel key -> fn(sales_order, reference) -> bool. Asked only after
#: the issuer failed. One working rail is enough to sell (operator rule, 14 Oct
#: 2026): the order goes through on that rail; registering the same number
#: with the issuer is queued under Waiting for TCB (payments.tcb_catch_up).
_REGISTER_FALLBACKS = {
	"Malipopay": "landms.malipopay.can_take_payment_now",
}


def register_reference_on_submit(sales_order_name: str, control_number: str) -> dict[str, Any]:
	"""The Sales Order submit gate: register with the issuer; if the issuer
	cannot, let the order through when another ticked rail can take payment on
	the number now. Only the submit gate uses this — Retry Registration and
	Reopen register with the issuer itself (register_reference_for_sales_order)."""
	import frappe

	channel = _channel_for_sales_order(sales_order_name)
	result = channel.register_reference(sales_order_name, control_number)
	if result.get("ok"):
		return result
	for key, method in _REGISTER_FALLBACKS.items():
		if key == channel.key:
			continue
		try:
			if frappe.get_attr(method)(sales_order_name, control_number):
				return {
					"ok": True,
					"status": "Issuer Pending",
					"issuer": channel.key,
					"via": key,
					"message": "{0} could not register {1} ({2}); {3} takes payment on it meanwhile "
					"until {0} is back (queued under Waiting for TCB).".format(
						channel.key, control_number, result.get("message"), key
					),
				}
		except Exception:
			frappe.log_error(frappe.get_traceback(), f"{key} submit fallback failed: {control_number}")
	return result


def decline_reference_for_sales_order(
	sales_order_name: str, control_number: str, final: bool = False
) -> dict[str, Any]:
	"""Decline / release a payment reference (SO-cancel/close gate) through the
	channel that owns it. ``final`` (contract termination) tells the following
	rails the order can never be reopened; the issuer's decline is the same.

	The stop may go ahead while the issuer is down (operator rule, 14 Oct
	2026) only when ALL hold — otherwise the issuer stays the gate, as before:
	  * the issuer could not be reached (no answer or a server error) — a
	    clear refusal still blocks;
	  * the order's project also collects through Malipopay (TCB-only
	    projects behave exactly as before);
	  * the issuer never confirmed the number;
	  * no payment ever arrived on the number (it would be live there).
	The decline is then queued under Waiting for TCB (payments.tcb_catch_up)."""
	import frappe

	from landms.payments.registry import channel_for_reference

	channel = channel_for_reference(control_number) or _channel_for_sales_order(sales_order_name)
	result = channel.decline_reference(sales_order_name, control_number)
	if not result.get("ok") and _may_stop_while_issuer_down(
		channel, sales_order_name, control_number, result
	):
		from landms.payments import tcb_catch_up

		tcb_catch_up.add(
			"Decline",
			sales_order_name,
			control_number,
			f"Stopped while {channel.key} was not answering; {channel.key} never confirmed the number, so it is "
			f"switched off there once {channel.key} is back.",
		)
		frappe.msgprint(
			f"{channel.key} is not answering, but it never confirmed payment number {control_number}, so this goes ahead. "
			f"Switching it off at {channel.key} is listed under Waiting for TCB and is finished there once {channel.key} "
			"is back.",
			indicator="orange",
			alert=True,
		)
		result = {
			"ok": True,
			"status": "Decline Deferred",
			"message": f"{channel.key} unreachable; {control_number} was never confirmed there — queued under Waiting "
			"for TCB.",
		}
	if result.get("ok"):
		_followers_decline(channel.key, sales_order_name, control_number, final=final)
	return result


def _may_stop_while_issuer_down(
	channel, sales_order_name: str, control_number: str, result: dict[str, Any]
) -> bool:
	"""See decline_reference_for_sales_order. Any doubt answers False, which
	keeps today's behaviour: the stop waits for the issuer."""
	if not result.get("unreachable"):
		return False
	never_registered = getattr(channel, "never_registered", None)
	payment_ever_arrived = getattr(channel, "payment_ever_arrived", None)
	if not never_registered or not payment_ever_arrived:
		return False
	try:
		from landms.malipopay import collects_via_malipopay

		if not collects_via_malipopay(sales_order_name):
			return False
		if not _sold_while_issuer_down(control_number):
			return False
		return bool(never_registered(control_number)) and not payment_ever_arrived(control_number)
	except Exception:
		import frappe

		frappe.log_error(
			frappe.get_traceback(), "Stop-while-issuer-down check failed: " + str(control_number)
		)
		return False


def _sold_while_issuer_down(control_number: str) -> bool:
	"""Only an order sold through another rail while the issuer was down may be
	stopped before the issuer confirms: it has a Register step in Waiting for
	TCB (whatever that step's status now). Numbers from before the Malipopay
	go-live never have one, so they keep today's behaviour."""
	import frappe

	from landms.payments.tcb_catch_up import QUEUE

	if not frappe.db.table_exists(QUEUE):
		return False
	return bool(frappe.db.exists(QUEUE, {"control_number": control_number, "action": "Register"}))


#: Rails that also collect a number they did not issue: channel key -> the
#: function that stops their copy, called as fn(sales_order, reference, reason, final=...).
#: Malipopay pauses the bill of an order that holds money (it may be reopened)
#: and expires it otherwise — always when the stop is final (a termination).
#: The issuer's decline is always the gate; once it succeeds every OTHER rail
#: listed here follows, and the issuer never follows itself. TCB issues today,
#: so Malipopay follows TCB. If a project's numbers were ever issued by
#: Malipopay, its decline would be the gate and TCB would follow — TCB's entry
#: is added together with that issuer channel (no Malipopay issuer exists yet).
_DECLINE_FOLLOWERS = {
	"Malipopay": "landms.malipopay.follow_decline_for_sales_order",
}


def _followers_decline(
	issuer_key: str, sales_order_name: str, control_number: str, final: bool = False
) -> None:
	"""Kill the number on every following rail. Never affects the decline result."""
	import frappe

	for key, method in _DECLINE_FOLLOWERS.items():
		if key == issuer_key:
			continue
		try:
			frappe.get_attr(method)(
				sales_order_name,
				control_number,
				f"Payment number {control_number} declined at {issuer_key}",
				final=final,
			)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"{key} follow-decline failed (decline unaffected): {control_number}",
			)


def generate_control_number(
	sales_order_name: str | None = None, *, related: bool = False, pattern: str | None = None
) -> str:
	"""Mint a payment reference for a Sales Order through the project's issuing
	channel. ``pattern`` is forwarded untouched (TCB derives it from the Land
	Acquisition when None, exactly as before)."""
	return _channel_for_sales_order(sales_order_name).generate_reference(
		sales_order_name, related=related, pattern=pattern
	)
