# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""Saving a submitted document must not fail on rounding noise.

After a save, the browser keeps a figure such as a plot's allocated cost with
more decimals than the database stores (…4067 against …407). The next
"Update" after submit sends it back and Frappe refuses: "Not allowed to change
Allocated Cost after submission from 1580736.846306407 to 1580736.8463064067".
The change the user actually made (an allow-on-submit field such as the
90-day tick) never saves.

Frappe runs before_update_after_submit BEFORE that comparison. Putting the
stored value back on a locked number field whose difference is only rounding
noise lets the real change through. Any real change to a locked field still
gets Frappe's error, and the official bypass flag is respected.
"""

import frappe
from frappe.model import float_like_fields

# Far below anything meaningful (a millionth of a shilling), far above the
# rounding noise seen (3e-10).
ROUNDING_NOISE = 1e-6


def drop_rounding_noise_on_locked_fields(doc):
	"""On a submitted ``doc``, put back the stored value of every locked number
	field that differs from it only by rounding noise."""
	if doc.flags.ignore_validate_update_after_submit:
		return
	stored = frappe.get_doc(doc.doctype, doc.name)
	for df in doc.meta.fields:
		if df.allow_on_submit or df.is_virtual or df.fieldtype not in float_like_fields:
			continue
		new, old = doc.get(df.fieldname), stored.get(df.fieldname)
		if new is None or old is None or new == old:
			continue
		try:
			noise = abs(float(new) - float(old)) < ROUNDING_NOISE
		except (TypeError, ValueError):
			continue  # not a number: leave it to Frappe's own check
		if noise:
			doc.set(df.fieldname, old)
