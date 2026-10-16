import time

import frappe
from frappe.utils import add_days, cint, cstr, flt, getdate, today

# The nightly run must get through even when something else is busy at
# midnight (the monthly depreciation posting, a slow TCB) — operator, 14 Oct
# 2026. Each order is its own unit, saved or undone on its own; a database
# that is busy (lock wait timeout / deadlock — the 1 Sep failures) is waited
# out and tried again; and a run that nears its time limit hands the rest to a
# follow-up job instead of being killed half-way (the 9 Oct JobTimeoutException).
LOCK_ATTEMPTS = 3
LOCK_PAUSE_SECONDS = 20
RUN_BUDGET_SECONDS = 20 * 60  # the long queue allows 25 minutes
NIGHTLY_LOCK = "landms:nightly_run"


def start_nightly_run():
	"""01:00 (hooks: cron) — hands the nightly run to the long queue.

	Not midnight: TCB is often unreachable 00:01–00:07 and other midnight jobs
	post ledger entries; and the TCB reconciliation (00:00) records any missed
	payment before anything is expired. A timed job itself runs in the default
	queue, which stops a job after 5 minutes — hence the hand-over."""
	frappe.enqueue("landms.tasks.daily", queue="long", job_id="landms-nightly-run", deduplicate=True)


def daily(attempted=None):
	"""Nightly housekeeping for the reservation window and overdue contracts.

	Runs in the long queue (started by start_nightly_run). ``attempted``
	carries the orders a previous part of tonight's run already handled, when
	this is a follow-up job."""
	# One pass at a time: a run pressed by hand while a follow-up job is still
	# working waits for it, instead of closing the same orders alongside it.
	if not _take_nightly_lock():
		frappe.log_error(
			"LandMS: nightly run not started",
			"Another pass of the nightly run was still working after 60 seconds.",
		)
		frappe.db.commit()  # keep the log
		return
	try:
		left = _run_nightly_jobs(attempted)
	finally:
		_release_nightly_lock()
	if left is not None:
		# the rest goes to a follow-up job, queued once the lock is free; what
		# was handled tonight is not repeated
		frappe.enqueue("landms.tasks.daily", queue="long", attempted=left)


def _run_nightly_jobs(attempted):
	"""Returns the orders handled so far when the run is out of time, else None."""
	started = time.monotonic()
	attempted = set(attempted or [])
	for job_name, job_fn in [
		("auto_cancel_stale_unpaid_applications", auto_cancel_stale_unpaid_applications),
		("auto_expire_paid_applications_past_deadline", auto_expire_paid_applications_past_deadline),
		("auto_process_overdue_plot_contracts", auto_process_overdue_plot_contracts),
	]:
		try:
			if job_fn(attempted=attempted, started=started) == "out of time":
				return sorted(attempted)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"LandMS daily: {job_name} failed")
			frappe.db.commit()  # keep the log: the next order's rollback would undo it
	return None


def _take_nightly_lock():
	# A named lock, not a row lock: it survives the per-order commits and is
	# released when the job's database connection closes, even after a crash.
	got = frappe.db.sql("SELECT GET_LOCK(%s, %s)", (NIGHTLY_LOCK, 60))
	return bool(got and got[0] and got[0][0] == 1)


def _release_nightly_lock():
	frappe.db.sql("SELECT RELEASE_LOCK(%s)", (NIGHTLY_LOCK,))


def _out_of_time(started):
	return started is not None and time.monotonic() - started > RUN_BUDGET_SECONDS


def _process_one(label, work):
	"""Do one order's work and save it on its own. A busy database (lock wait
	timeout, deadlock) is waited out and tried again; any other failure — or
	a database still busy after LOCK_ATTEMPTS — is undone and logged, and the
	order is tried again on the next run. Returns True when it was done."""
	for attempt in range(1, LOCK_ATTEMPTS + 1):
		try:
			work()
			frappe.db.commit()  # per-order: persist this success on its own
			return True
		except (frappe.QueryTimeoutError, frappe.QueryDeadlockError):
			frappe.db.rollback()
			if attempt == LOCK_ATTEMPTS:
				frappe.log_error(
					frappe.get_traceback(),
					f"LandMS: Failed to {label} — the database stayed busy after {LOCK_ATTEMPTS} tries",
				)
				frappe.db.commit()  # keep the log: the next order's rollback would undo it
				return False
			time.sleep(LOCK_PAUSE_SECONDS)
		except Exception:
			# Roll back this order's partial writes BEFORE logging, so a mid-operation
			# crash cannot be flushed to the DB by a later commit (leaving a half-done
			# state). The order stays in its original state and is retried next run.
			frappe.db.rollback()
			frappe.log_error(frappe.get_traceback(), f"LandMS: Failed to {label}")
			frappe.db.commit()  # keep the log: the next order's rollback would undo it
			return False
	return False


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


def auto_cancel_stale_unpaid_applications(attempted=None, started=None):
	attempted = attempted if attempted is not None else set()
	settings = frappe.get_single("LandMS Settings")
	expiry_days = int(settings.unpaid_application_expiry_days or 3)
	cutoff_date = add_days(today(), -expiry_days)

	for app in _unpaid_applications_older_than(cutoff_date):
		if app.name in attempted:
			continue
		if _out_of_time(started):
			return "out of time"
		attempted.add(app.name)

		def cancel(app=app):
			doc = frappe.get_doc("Plot Application", app.name)
			doc.flags.ignore_permissions = True
			doc.cancel()

		if _process_one(f"cancel Plot Application {app.name}", cancel):
			frappe.logger("landms").info(
				f"Auto-cancelled unpaid Plot Application {app.name} "
				f"(plot {app.plot}, customer {app.customer}) — "
				f"no payment received within {expiry_days} days"
			)


def _unpaid_applications_older_than(cutoff_date):
	return frappe.db.get_all(
		"Plot Application",
		filters={
			"docstatus": 1,
			"status": "Submitted",
			"application_date": ["<=", cutoff_date],
		},
		fields=["name", "plot", "customer"],
	)


def auto_expire_paid_applications_past_deadline(attempted=None, started=None):
	"""Expire paid applications whose reservation window ended before first advance.

	The application fee is non-refundable.
	  - No advance received: the plot is released and the unpaid Sales Order /
	    draft contract chain is torn down (the application is simply cancelled).
	  - PARTIAL advance received: the reservation is closed through the same
	    forfeiture-aware path as the Close button (close_sales_order) — the partial
	    advance is forfeited (net of the government share), a credit note clears the
	    invoice outstanding, the TCB control number is declined, the draft contract
	    is removed, and the plot released. This avoids freeing the plot while a
	    submitted SO / paid invoice / draft contract are left dangling.
	"""
	attempted = attempted if attempted is not None else set()
	settings = frappe.get_single("LandMS Settings")
	today_date = getdate(today())
	validity_days = int(settings.application_fee_validity_days or 7)

	for app in _paid_applications_past_deadline(validity_days, today_date):
		if app.name in attempted:
			continue
		if _out_of_time(started):
			return "out of time"
		attempted.add(app.name)

		active_contract = _active_contract(app.sales_order)
		if active_contract:
			# The advance was met and the contract is running — only the
			# application's status was left at "Paid" (payments moved by hand skip
			# the step that marks it Converted). Closing would be refused every
			# night (live: PA-2026-0713, PA-2026-2191), so leave it alone.
			frappe.logger("landms").info(
				f"Skipped expiring Plot Application {app.name}: Plot Contract "
				f"{active_contract} is already submitted (application status is stale)"
			)
			continue

		if _sales_order_has_advance_payment(app.sales_order):
			# Money is in → close via the forfeiture-aware flow (same as the
			# Close button): forfeits net of govt, credit-notes the outstanding,
			# declines the TCB control number, releases the plot, cancels the app.
			def close(app=app):
				from landms.sales_order_hooks import close_sales_order

				close_sales_order(app.sales_order)

			if _process_one(f"expire Plot Application {app.name}", close):
				frappe.logger("landms").info(
					f"Auto-expired Plot Application {app.name} via close_sales_order "
					f"(plot {app.plot}, expired {app.expiry_date}) — partial advance "
					"forfeited, Sales Order closed, plot released"
				)
		elif _process_one(
			f"expire Plot Application {app.name}", lambda app=app: _expire_without_advance(app)
		):
			frappe.logger("landms").info(
				f"Auto-expired Plot Application {app.name} "
				f"(plot {app.plot}, expired {app.expiry_date}) — "
				"first advance was not received in time"
			)


def _paid_applications_past_deadline(validity_days, today_date):
	"""Paid applications whose advance window has ended.

	Per-plot rule: a plot flagged no_advance_deadline uses its FULL payment
	window (payment_completion_days, from the Land Acquisition) as the advance
	window instead of the global setting. Plots without the flag (all existing
	plots) behave exactly as before."""
	return frappe.db.sql(
		"""
		select
			pa.name,
			pa.plot,
			pa.sales_order,
			date_add(pa.payment_date, interval (
				case when ifnull(pm.no_advance_deadline, 0) = 1
				     then coalesce(nullif(pm.payment_completion_days, 0), %s)
				     else %s end
			) day) as expiry_date
		from `tabPlot Application` pa
		left join `tabPlot Master` pm on pm.name = pa.plot
		where pa.docstatus = 1
		  and pa.status = 'Paid'
		  and pa.payment_date is not null
		  and date_add(pa.payment_date, interval (
				case when ifnull(pm.no_advance_deadline, 0) = 1
				     then coalesce(nullif(pm.payment_completion_days, 0), %s)
				     else %s end
			) day) < %s
		order by pa.payment_date asc, pa.name asc
		""",
		(validity_days, validity_days, validity_days, validity_days, today_date),
		as_dict=True,
	)


def _expire_without_advance(app):
	"""No advance received: release the payment number FIRST, then cancel the
	application (which cancels its unpaid order and frees the plot).

	Cancelling the application first left orders half-done when TCB was down at
	midnight (3–5 Oct 2026): the application was cancelled and the plot freed —
	the TCB logger commits mid-way — while the order's own cancel failed at its
	decline gate, so the order stayed open with a live number, and the nightly
	run never looked at it again. Now, if TCB does not release the number,
	nothing at all is changed and the order is tried again on the next run; once
	it is released, the order's cancel finds it already declined."""
	so = app.sales_order
	if so and frappe.db.get_value("Sales Order", so, "docstatus") == 1:
		number = cstr(frappe.db.get_value("Sales Order", so, "control_number")).strip()
		if number:
			from landms.sales_order_hooks import decline_reference_for_sales_order

			result = decline_reference_for_sales_order(so, number)
			if not result.get("ok"):
				frappe.throw(
					f"The bank did not release payment number {number} ({result.get('message')}). "
					"Nothing was changed; the order is tried again on the next run."
				)
	doc = frappe.get_doc("Plot Application", app.name)
	doc.flags.ignore_permissions = True
	doc.flags._cancellation_reason = "Expired"
	doc.cancel()


def _active_contract(so_name):
	"""The submitted Plot Contract on this order, if there is one."""
	if not so_name:
		return None
	return frappe.db.get_value("Plot Contract", {"sales_order": so_name, "docstatus": 1}, "name")


def _sales_order_has_advance_payment(so_name):
	"""True if the application's submitted Sales Order has received any advance on
	its plot Sales Invoice (outstanding < grand_total). Draft/missing SO → False."""
	if not so_name or not frappe.db.exists("Sales Order", so_name):
		return False
	if frappe.db.get_value("Sales Order", so_name, "docstatus") != 1:
		return False
	invoice = frappe.db.get_value("Sales Order", so_name, "plot_sales_invoice")
	if not invoice or not frappe.db.exists("Sales Invoice", invoice):
		return False
	grand_total, outstanding = frappe.db.get_value(
		"Sales Invoice", invoice, ["grand_total", "outstanding_amount"]
	)
	return flt(grand_total) - flt(outstanding) > 0


def auto_process_overdue_plot_contracts(attempted=None, started=None):
	"""After first advance, govern overdue contracts by payment_deadline.

	Once the first advance has been fully met, the paid-application validity
	window is no longer in control. From that point forward the repayment
	timeline is owned by the contract's payment_deadline. Missed deadlines:

	  - always mark the contract Overdue
	  - only auto-terminate when Apply Auto-Cancellation is enabled
	"""
	attempted = attempted if attempted is not None else set()
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

	for row in stale_contracts:
		if row.name in attempted:
			continue
		if _out_of_time(started):
			return "out of time"
		attempted.add(row.name)
		_process_one(
			f"process overdue Plot Contract {row.name}", lambda row=row: _process_overdue_contract(row)
		)


def _process_overdue_contract(row):
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
		frappe.logger("landms").info(
			f"Marked Plot Contract {row.name} overdue "
			f"(plot {row.plot}, customer {row.customer}) — "
			"awaiting manual action because auto-cancellation is off"
		)
		return

	contract = frappe.get_doc("Plot Contract", row.name)
	if contract.contract_status != "Overdue":
		contract.db_set("contract_status", "Overdue", update_modified=True)

	# terminate_contract declines the number first; if that fails nothing is
	# changed and the contract is tried again on the next run
	contract.terminate_contract("Payment deadline missed — auto-cancelled by scheduler.")
	frappe.logger("landms").info(
		f"Auto-terminated overdue Plot Contract {row.name} "
		f"(plot {row.plot}, customer {row.customer}) — "
		f"payment deadline {row.payment_deadline} was missed"
	)


def auto_cancel_stale_open_sales_orders_without_payment():
	"""Backward-compatible wrapper for the old scheduler entry point."""
	return auto_process_overdue_plot_contracts()
