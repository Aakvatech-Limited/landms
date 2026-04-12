import frappe
from frappe.utils import add_days, cint, flt, getdate, today


def daily():
	"""Daily housekeeping for the pre-contract reservation window."""
	for job_name, job_fn in [
		("auto_cancel_stale_unpaid_applications", auto_cancel_stale_unpaid_applications),
		("auto_expire_paid_applications_past_deadline", auto_expire_paid_applications_past_deadline),
		("auto_cancel_stale_open_sales_orders_without_payment", auto_cancel_stale_open_sales_orders_without_payment),
	]:
		try:
			job_fn()
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"LandMS daily: {job_name} failed",
			)


def auto_cancel_stale_unpaid_applications():
	settings = frappe.get_single("LandMS Settings")
	expiry_days = int(settings.unpaid_application_expiry_days or 3)
	cutoff_date = add_days(today(), -expiry_days)

	stale_apps = frappe.db.get_all(
		"Plot Application",
		filters={
			"docstatus": 1,
			"status": "Submitted",
			"application_date": ["<=", cutoff_date],
		},
		fields=["name", "plot", "customer"],
	)

	cancelled_count = 0
	for app in stale_apps:
		try:
			doc = frappe.get_doc("Plot Application", app.name)
			doc.flags.ignore_permissions = True
			doc.cancel()
			cancelled_count += 1
			frappe.logger("landms").info(
				f"Auto-cancelled unpaid Plot Application {app.name} "
				f"(plot {app.plot}, customer {app.customer}) — "
				f"no payment received within {expiry_days} days"
			)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"LandMS: Failed to cancel Plot Application {app.name}",
			)

	if cancelled_count:
		frappe.db.commit()


def auto_expire_paid_applications_past_deadline():
	"""Cancel paid applications whose first-advance window has expired.

	The application fee is non-refundable. Cancelling the application here
	only releases the plot and tears down the unpaid Sales Order / draft
	contract chain.
	"""
	settings = frappe.get_single("LandMS Settings")
	today_date = getdate(today())
	validity_days = int(settings.application_fee_validity_days or 7)

	expired_apps = frappe.db.sql(
		"""
		select
			name,
			plot,
			date_add(payment_date, interval %s day) as expiry_date
		from `tabPlot Application`
		where docstatus = 1
		  and status = 'Paid'
		  and apply_auto_cancellation = 1
		  and payment_date is not null
		  and date_add(payment_date, interval %s day) < %s
		order by payment_date asc, name asc
		""",
		(validity_days, validity_days, today_date),
		as_dict=True,
	)

	expired_count = 0
	for app in expired_apps:
		try:
			doc = frappe.get_doc("Plot Application", app.name)
			doc.flags.ignore_permissions = True
			doc.flags._cancellation_reason = "Expired"
			doc.cancel()
			expired_count += 1
			frappe.logger("landms").info(
				f"Auto-expired Plot Application {app.name} "
				f"(plot {app.plot}, expired {app.expiry_date}) — "
				"first advance was not received in time"
			)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"LandMS: Failed to expire Plot Application {app.name}",
			)

	if expired_count:
		frappe.db.commit()


def auto_cancel_stale_open_sales_orders_without_payment():
	"""Mark overdue + optionally cancel plot SOs past the app validity window.

	Always marks the Plot Contract as Overdue when the deadline has passed.
	Only cancels the SO if the Plot Contract has apply_auto_cancellation on.
	"""
	settings = frappe.get_single("LandMS Settings")
	validity_days = int(settings.application_fee_validity_days or 7)
	today_date = getdate(today())

	stale_orders = frappe.db.sql(
		"""
		select
			so.name,
			so.plot,
			so.customer,
			so.plot_application,
			so.plot_contract,
			date_add(app.payment_date, interval %s day) as expiry_date
		from `tabSales Order` so
		inner join `tabPlot Application` app
			on app.name = so.plot_application
		where so.docstatus = 1
		  and ifnull(so.plot_application, '') != ''
		  and ifnull(so.plot, '') != ''
		  and app.docstatus = 1
		  and app.status = 'Paid'
		  and app.payment_date is not null
		  and date_add(app.payment_date, interval %s day) < %s
		order by app.payment_date asc, so.name asc
		""",
		(validity_days, validity_days, today_date),
		as_dict=True,
	)

	cancelled_count = 0
	overdue_count = 0
	for row in stale_orders:
		try:
			so = frappe.get_doc("Sales Order", row.name)
			invoice_name = so.get("plot_sales_invoice")
			if invoice_name and frappe.db.exists("Sales Invoice", invoice_name):
				outstanding, grand_total = frappe.db.get_value(
					"Sales Invoice",
					invoice_name,
					["outstanding_amount", "grand_total"],
				)
				if flt(outstanding) < flt(grand_total):
					continue

			if frappe.db.get_value("Plot Application", row.plot_application, "docstatus") != 1:
				continue

			# Always mark the Plot Contract as Overdue.
			auto_cancel = True
			if row.plot_contract and frappe.db.exists("Plot Contract", row.plot_contract):
				contract_status = frappe.db.get_value(
					"Plot Contract", row.plot_contract,
					["contract_status", "apply_auto_cancellation"],
					as_dict=True,
				)
				if contract_status and contract_status.contract_status in ("Draft", "Ongoing"):
					frappe.db.set_value(
						"Plot Contract", row.plot_contract,
						"contract_status", "Overdue",
						update_modified=True,
					)
					overdue_count += 1
				auto_cancel = cint(contract_status.apply_auto_cancellation) if contract_status else True

			if not auto_cancel:
				continue

			so.flags.ignore_permissions = True
			so.cancel()
			cancelled_count += 1
			frappe.logger("landms").info(
				f"Auto-cancelled stale Sales Order {row.name} "
				f"(plot {row.plot}, customer {row.customer}) — "
				f"no first advance received within {validity_days} days"
			)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"LandMS: Failed to process stale Sales Order {row.name}",
			)

	if cancelled_count:
		frappe.db.commit()
