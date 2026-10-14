# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Borrow Administrator for one step without losing the caller's login.

frappe.set_user() swaps the user AND replaces frappe.session.sid and
frappe.session.data. Switching back with frappe.set_user(original_user) puts
the name back but not the rest, and at the end of a browser request Frappe
saves that emptied session to its cache — the user's next click then fails
with "User None is disabled". These two helpers give the caller back their
whole session. Who the step runs as, and what it writes, is unchanged.
set_user() also empties frappe.form_dict, so read the request's values before
the Administrator step, not after it.

	caller = become_administrator()
	try:
		...
	finally:
		restore_caller(caller)
"""

import frappe


def become_administrator():
	"""Switch to Administrator. Returns what restore_caller() needs."""
	session = frappe.local.session
	caller = (session.user, session.get("sid"), session.get("data"))
	frappe.set_user("Administrator")
	return caller


def restore_caller(caller):
	"""Switch back to the caller, with their whole session."""
	user, sid, data = caller
	frappe.set_user(user)
	session = frappe.local.session
	if sid is not None:
		session.sid = sid
	if data is not None:
		session.data = data
