# Copyright (c) 2026, Aakvatech and contributors
# For license information, please see license.txt

"""The public receipt check page: /verify and /verify/<code>.

The page itself holds no data. Its script reads the code from the address
(or the box) and asks landms.receipt_verification.verify_receipt, which is
read-only, rate-limited and logged."""

import frappe

no_cache = 1
sitemap = 0


def get_context(context):
	context.no_header = 1
	context.no_breadcrumbs = 1
	# only a signed-in desk user has a CSRF token; officers are guests and need none
	context.csrf_token = frappe.session.data.csrf_token if frappe.session.user != "Guest" else ""
	return context
