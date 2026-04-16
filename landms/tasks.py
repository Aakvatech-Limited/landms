import frappe
from frappe.utils import add_days, cint, flt, getdate, today


def daily():
	"""Daily housekeeping for the pre-contract reservation window."""
	for job_name, job_fn in [
		("auto_cancel_stale_unpaid_applications", auto_cancel_stale_unpaid_applications),
		("auto_expire_paid_applications_past_deadline", auto_expire_paid_applications_past_deadline),
		("auto_process_overdue_plot_contracts", auto_process_overdue_plot_contracts),
	]:
		try:
			job_fn()
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"LandMS daily: {job_name} failed",
			)


def run_daily_tcb_reconciliation():
	"""Nightly TCB reconciliation job — wired to daily_long scheduler."""
	try:
		from landms.tcb import run_tcb_reconciliation_job
		run_tcb_reconciliation_job(triggered_by="Scheduled")
	except Exception:
		frappe.log_error(
			frappe.get_traceback(),
			"LandMS: TCB nightly reconciliation failed",
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
	"""Expire paid applications whose reservation window ended before first advance.

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


def auto_process_overdue_plot_contracts():
	"""After first advance, govern overdue contracts by payment_deadline.

	Once the first advance has been fully met, the paid-application validity
	window is no longer in control. From that point forward the repayment
	timeline is owned by the contract's payment_deadline. Missed deadlines:

	  - always mark the contract Overdue
	  - only auto-terminate when Apply Auto-Cancellation is enabled
	"""
	today_date = getdate(today())

	stale_contracts = frappe.db.get_all(
		"Plot Contract",
		filters={
			"docstatus": 1,
			"contract_status": ["in", ["Ongoing", "Overdue"]],
			"payment_deadline": ["<", today_date],
			"total_outstanding": [">", 0],
		},
		fields=[
			"name",
			"sales_order",
			"plot",
			"customer",
			"contract_status",
			"apply_auto_cancellation",
			"payment_deadline",
		],
	)

	terminated_count = 0
	overdue_count = 0
	for row in stale_contracts:
		try:
			auto_cancel = cint(row.apply_auto_cancellation)
			if row.sales_order and frappe.db.exists("Sales Order", row.sales_order):
				so_auto_cancel = frappe.db.get_value(
					"Sales Order",
					row.sales_order,
					"apply_auto_cancellation",
				)
				if so_auto_cancel is not None:
					auto_cancel = cint(so_auto_cancel)
					if cint(row.apply_auto_cancellation) != auto_cancel:
						frappe.db.set_value(
							"Plot Contract",
							row.name,
							"apply_auto_cancellation",
							auto_cancel,
							update_modified=False,
						)

			if not auto_cancel:
				if row.contract_status != "Overdue":
					frappe.db.set_value(
						"Plot Contract",
						row.name,
						"contract_status",
						"Overdue",
						update_modified=True,
					)
					overdue_count += 1
				frappe.logger("landms").info(
					f"Marked Plot Contract {row.name} overdue "
					f"(plot {row.plot}, customer {row.customer}) — "
					"awaiting manual action because auto-cancellation is off"
				)
				continue

			contract = frappe.get_doc("Plot Contract", row.name)
			if contract.contract_status != "Overdue":
				contract.db_set("contract_status", "Overdue", update_modified=True)
				overdue_count += 1

			contract.terminate_contract("Payment deadline missed — auto-cancelled by scheduler.")
			terminated_count += 1
			frappe.logger("landms").info(
				f"Auto-terminated overdue Plot Contract {row.name} "
				f"(plot {row.plot}, customer {row.customer}) — "
				f"payment deadline {row.payment_deadline} was missed"
			)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"LandMS: Failed to process overdue Plot Contract {row.name}",
			)

	if overdue_count or terminated_count:
		frappe.db.commit()


def auto_cancel_stale_open_sales_orders_without_payment():
	"""Backward-compatible wrapper for the old scheduler entry point."""
	return auto_process_overdue_plot_contracts()
