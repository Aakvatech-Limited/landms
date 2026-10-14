"""Payment-channel abstraction for landms.

landms collects plot payments through external channels that mint a reference
(a "control number") when a Sales Order is submitted, then push an inbound
callback (IPN) when the customer pays. Today there is exactly one channel — TCB.
Malipopay (and possibly others) are coming.

This package lets several channels coexist without double-processing and without
scattering ``if channel == "..."`` logic across the codebase:

  * ``base``         — the PaymentChannel contract every channel implements.
  * ``registry``     — the key -> adapter map, plus the resolvers that decide
                       which channel a Land Acquisition (or an existing
                       reference) belongs to.
  * ``tcb_channel``  — the TCB adapter. A PURE FORWARDER: every method returns
                       the result of the existing ``landms.tcb`` function
                       unchanged. No behaviour moves here.

Design note: the shape is borrowed from ERPNext's Payment Gateway pattern
(a registry + one controller per gateway resolved by a stable key + a
data-driven per-project selector), but the interface verbs are landms' own —
these are collection/IPN channels, not checkout/redirect gateways.

IMPORTANT (rollout): importing this package registers the built-in channels. The
Sales Order hooks route register / decline / mint through ``router``, which
resolves the channel from the Land Acquisition ("Control Numbers Issued By").
Every project defaults to TCB and TCBChannel is a pure forwarder, so the live
TCB path runs exactly as before. The Land Acquisition is the only switch —
there is no global flag.
"""

from landms.payments import tcb_channel  # noqa: F401  (registers TCBChannel on import)
