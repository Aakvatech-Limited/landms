from __future__ import annotations

import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, escape_html, flt, get_url_to_form, getdate, today

from landms.landms.doctype.land_acquisition.land_acquisition import (
	sync_land_acquisition_plot_summary,
)


class PlotContract(Document):
	# ------------------------------------------------------------------ #
	#  Validate / Submit / Cancel                                          #
	# ------------------------------------------------------------------ #

	def validate(self):
		self.validate_plot_available()
		self.fill_selling_price()
		self.calculate_financials()
		self.generate_payment_schedule()
		self.calculate_payment_summary()

	def before_submit(self):
		self._validate_sales_order_first_payment_gate()

	def before_cancel(self):
		if flt(self.total_paid) > 0:
			frappe.throw(
				f"Contract {self.name} has received payments (TZS {flt(self.total_paid):,.0f}). "
				"Use Terminate Contract instead of Cancel."
			)

	def on_submit(self):
		frappe.db.set_value("Plot Master", self.plot, "status", "Reserved")
		self._sync_land_acquisition_summary()
		self.db_set("contract_status", "Ongoing")

	def on_cancel(self):
		frappe.db.set_value("Plot Master", self.plot, "status", "Available")
		self._sync_land_acquisition_summary()
		self.db_set("contract_status", "Cancelled")

	# ------------------------------------------------------------------ #
	#  Validation helpers                                                  #
	# ------------------------------------------------------------------ #

	def validate_plot_available(self):
		if not self.plot:
			return
		if self.sales_order or self.flags.get("from_sales_order"):
			return
		if frappe.db.exists("Plot Contract", self.name):
			return

		plot_status = frappe.db.get_value("Plot Master", self.plot, "status")
		if plot_status != "Available":
			frappe.throw(
				f"Plot {self.plot} is not Available (current status: {plot_status}). "
				"Only Available plots can be contracted."
			)
		active = frappe.db.exists("Plot Contract", {
			"plot": self.plot,
			"docstatus": 1,
			"contract_status": ["in", ["Ongoing", "Completed"]],
		})
		if active:
			frappe.throw(
				f"Plot {self.plot} already has an active contract ({active}). "
				"Terminate or complete it first."
			)

	def _validate_sales_order_first_payment_gate(self):
		if not self.sales_order:
			return

		if not frappe.db.exists("Sales Order", self.sales_order):
			frappe.throw(f"Linked Sales Order {self.sales_order} was not found.")

		so_doc = frappe.get_doc("Sales Order", self.sales_order)
		if so_doc.docstatus != 1:
			frappe.throw(
				f"Linked Sales Order {so_doc.name} is not submitted. "
				"Submit the Sales Order first."
			)
		if so_doc.plot != self.plot or so_doc.customer != self.customer:
			frappe.throw(
				f"Contract {self.name} does not match linked Sales Order {so_doc.name} "
				"(plot/customer mismatch)."
			)
		if not self._sales_order_has_any_confirmed_payment(so_doc):
			frappe.throw(
				f"Cannot submit contract {self.name} before first payment on Sales Order "
				f"{so_doc.name}. Record payment on the Sales Order first."
			)

	def _sales_order_has_any_confirmed_payment(self, so_doc):
		plot_invoice = so_doc.get("plot_sales_invoice")
		if not plot_invoice or not frappe.db.exists("Sales Invoice", plot_invoice):
			return bool(flt(so_doc.get("advance_paid")))

		si = frappe.db.get_value(
			"Sales Invoice",
			plot_invoice,
			["docstatus", "outstanding_amount", "grand_total"],
			as_dict=True,
		)
		return bool(si and si.docstatus == 1 and flt(si.outstanding_amount) < flt(si.grand_total))

	# ------------------------------------------------------------------ #
	#  Field calculation helpers                                           #
	# ------------------------------------------------------------------ #

	def fill_selling_price(self):
		if not self.plot:
			return

		plot_data = frappe.db.get_value(
			"Plot Master",
			self.plot,
			["selling_price", "land_acquisition"],
			as_dict=True,
		)
		if not plot_data:
			return

		if not flt(self.selling_price):
			self.selling_price = plot_data.selling_price
		self.land_acquisition = plot_data.land_acquisition
		if plot_data.land_acquisition:
			self.acquisition_name = frappe.db.get_value(
				"Land Acquisition",
				plot_data.land_acquisition,
				"acquisition_name",
			) or ""

		so_doc = self._get_sales_order_doc()
		if so_doc:
			self.control_number = so_doc.get("control_number") or self.control_number
			self.plot_application = so_doc.get("plot_application") or self.plot_application
			self.booking_fee_invoice = so_doc.get("plot_sales_invoice") or self.booking_fee_invoice

	def calculate_financials(self):
		if flt(self.selling_price) > 0 and flt(self.booking_fee_percent) > 0:
			self.booking_fee_amount = flt(self.selling_price) * flt(self.booking_fee_percent) / 100
			self.balance_due = flt(self.selling_price) - flt(self.booking_fee_amount)
		if self.contract_date and flt(self.payment_completion_days) > 0:
			self.payment_deadline = add_days(self.contract_date, int(self.payment_completion_days))

	def generate_payment_schedule(self):
		"""Only standalone manual contracts generate their own schedule.

		Sales-Order-linked contracts are read-only mirrors of the Sales Order /
		Sales Invoice schedule and are populated by sales_order_hooks + syncs.
		"""
		if self.docstatus == 1:
			return
		if self.sales_order or self.flags.get("from_sales_order"):
			return
		if not flt(self.selling_price) or not flt(self.booking_fee_percent):
			return
		if not self.contract_date:
			return

		booking_fee = flt(self.booking_fee_amount) or (
			flt(self.selling_price) * flt(self.booking_fee_percent) / 100
		)
		balance = flt(self.selling_price) - booking_fee
		total_days = int(self.payment_completion_days or 90)

		self.set("payment_schedule", [])
		self.append("payment_schedule", {
			"installment_number": 1,
			"description": "Advance",
			"due_date": self.contract_date,
			"expected_amount": booking_fee,
			"paid_amount": 0,
			"outstanding_amount": booking_fee,
			"paid_date": None,
			"sales_invoice": "",
			"status": "Pending",
		})

		if balance > 0:
			self.append("payment_schedule", {
				"installment_number": 2,
				"description": "Balance",
				"due_date": add_days(self.contract_date, total_days),
				"expected_amount": balance,
				"paid_amount": 0,
				"outstanding_amount": balance,
				"paid_date": None,
				"sales_invoice": "",
				"status": "Pending",
			})

	def calculate_payment_summary(self):
		self.total_contract_value = flt(self.selling_price)
		total_paid = sum(flt(row.paid_amount) for row in self.payment_schedule)
		self.total_paid = total_paid
		self.total_outstanding = max(0.0, flt(self.selling_price) - total_paid)
		self.payment_progress = self._derive_payment_progress(total_paid, self.total_outstanding)
		if flt(self.government_share_percent) > 0:
			self.government_fee_withheld = (
				flt(self.selling_price) * flt(self.government_share_percent) / 100
			)

	def _derive_payment_progress(self, total_paid, total_outstanding):
		if flt(total_paid) <= 0:
			return "Unpaid"
		if flt(total_outstanding) <= 0:
			return "Fully Paid"

		first_expected = 0.0
		first_paid = 0.0
		for row in self.payment_schedule:
			if cint(row.installment_number or 0) == 1:
				first_expected = flt(row.expected_amount)
				first_paid = flt(row.paid_amount)
				break

		if first_expected > 0 and first_paid >= first_expected:
			later_paid = sum(
				flt(row.paid_amount)
				for row in self.payment_schedule
				if cint(row.installment_number or 0) > 1
			)
			if later_paid > 0:
				return "Advance + Installments Paid"
			return "Advance Paid"

		return "Partially Paid"

	def _sync_land_acquisition_summary(self):
		la = self.land_acquisition or frappe.db.get_value(
			"Plot Master",
			self.plot,
			"land_acquisition",
		)
		if la:
			sync_land_acquisition_plot_summary(la)

	# ------------------------------------------------------------------ #
	#  Sales Order / Invoice link helpers                                  #
	# ------------------------------------------------------------------ #

	def _get_sales_order_doc(self):
		if not self.sales_order or not frappe.db.exists("Sales Order", self.sales_order):
			return None
		return frappe.get_doc("Sales Order", self.sales_order)

	def _get_plot_invoice_name(self):
		so_doc = self._get_sales_order_doc()
		if so_doc:
			invoice_name = so_doc.get("plot_sales_invoice") or ""
			if invoice_name and frappe.db.exists("Sales Invoice", invoice_name):
				return invoice_name
		if self.booking_fee_invoice and frappe.db.exists("Sales Invoice", self.booking_fee_invoice):
			return self.booking_fee_invoice
		return ""

	# ------------------------------------------------------------------ #
	#  Payment sync (single-SI mirror)                                     #
	# ------------------------------------------------------------------ #

	def sync_payment_status(self):
		so_doc = self._get_sales_order_doc()
		if not so_doc:
			return

		invoice_name = self._get_plot_invoice_name()
		if not invoice_name:
			self._sync_header_from_sales_order(so_doc)
			return

		si_doc = frappe.get_doc("Sales Invoice", invoice_name)
		if si_doc.docstatus != 1:
			return

		total_paid = max(0.0, flt(si_doc.grand_total) - flt(si_doc.outstanding_amount))
		total_outstanding = max(0.0, flt(si_doc.outstanding_amount))
		paid_dates = self._get_paid_dates_by_installment(si_doc)

		if len(self.payment_schedule or []) != len(si_doc.payment_schedule or []) and self.docstatus == 0:
			self._rebuild_schedule_from_invoice(si_doc)

		if total_paid > 0 and self.docstatus == 0:
			self.submit()
			self.reload()

		self._sync_header_from_sales_order(so_doc, invoice_name=si_doc.name)
		self._sync_schedule_rows_from_invoice(si_doc, paid_dates)
		self.db_set("total_contract_value", flt(si_doc.grand_total), update_modified=False)
		self.db_set("total_paid", total_paid, update_modified=False)
		self.db_set("total_outstanding", total_outstanding, update_modified=False)
		self.db_set(
			"payment_progress",
			self._derive_payment_progress(total_paid, total_outstanding),
			update_modified=False,
		)

		if so_doc.get("plot_application"):
			app_status = frappe.db.get_value("Plot Application", so_doc.plot_application, "status")
			if total_paid > 0 and app_status == "Paid":
				frappe.db.set_value(
					"Plot Application",
					so_doc.plot_application,
					"status",
					"Converted",
				)

		if total_outstanding <= 0 and self.docstatus == 1:
			self.db_set("contract_status", "Completed", update_modified=False)
			plot_status = frappe.db.get_value("Plot Master", self.plot, "status")
			if plot_status not in ("Delivered", "Title Closed"):
				frappe.db.set_value("Plot Master", self.plot, "status", "Ready for Handover")
			self._sync_land_acquisition_summary()

			settings = frappe.get_single("LandMS Settings")
			self.reload()
			je_name = self._post_completion_entries(settings)

			msg = f"Contract fully paid. Plot {self.plot} marked as Ready for Handover."
			if je_name:
				msg += f" Government fee posted — Journal Entry: {je_name}."
			frappe.msgprint(msg, indicator="green", alert=True)

		elif total_paid > 0 and self.docstatus == 1:
			self.db_set("contract_status", "Ongoing", update_modified=False)
			if frappe.db.get_value("Plot Master", self.plot, "status") == "Pending Advance":
				frappe.db.set_value("Plot Master", self.plot, "status", "Reserved")
				self._sync_land_acquisition_summary()

	def _sync_header_from_sales_order(self, so_doc, *, invoice_name: str | None = None):
		updates = {
			"plot_application": so_doc.get("plot_application") or "",
			"control_number": so_doc.get("control_number") or "",
			"booking_fee_invoice": invoice_name or so_doc.get("plot_sales_invoice") or "",
			"payment_deadline": so_doc.get("payment_deadline"),
		}
		frappe.db.set_value("Plot Contract", self.name, updates, update_modified=False)

	def _rebuild_schedule_from_invoice(self, invoice):
		self.set("payment_schedule", [])
		for idx, row in enumerate(invoice.get("payment_schedule") or [], start=1):
			expected = flt(row.payment_amount)
			paid_amount = flt(row.paid_amount or 0)
			outstanding = flt(row.outstanding or max(0.0, expected - paid_amount))
			self.append("payment_schedule", {
				"installment_number": idx,
				"description": row.description or self._default_installment_label(idx),
				"due_date": row.due_date,
				"expected_amount": expected,
				"paid_amount": paid_amount,
				"outstanding_amount": outstanding,
				"paid_date": None,
				"sales_invoice": invoice.name,
				"status": self._derive_installment_status(row.due_date, expected, outstanding),
			})
		self.save(ignore_permissions=True)

	def _sync_schedule_rows_from_invoice(self, invoice, paid_dates):
		today_date = getdate(today())
		rows = sorted(self.payment_schedule, key=lambda d: cint(d.installment_number or 0))

		for idx, source in enumerate(invoice.get("payment_schedule") or [], start=1):
			if idx > len(rows):
				break

			target = rows[idx - 1]
			expected = flt(source.payment_amount)
			paid_amount = flt(source.paid_amount or 0)
			outstanding = flt(source.outstanding or max(0.0, expected - paid_amount))
			status = self._derive_installment_status(source.due_date, expected, outstanding, today_date=today_date)
			paid_date = paid_dates.get(idx) if outstanding <= 0 else None

			frappe.db.set_value(
				"Plot Contract Payment",
				target.name,
				{
					"description": source.description or self._default_installment_label(idx),
					"due_date": source.due_date,
					"expected_amount": expected,
					"paid_amount": paid_amount,
					"outstanding_amount": outstanding,
					"paid_date": paid_date,
					"sales_invoice": invoice.name,
					"status": status,
				},
				update_modified=False,
			)

	def _derive_installment_status(self, due_date, expected, outstanding, *, today_date=None):
		today_date = today_date or getdate(today())
		if expected > 0 and outstanding <= 0:
			return "Paid"
		if due_date and getdate(str(due_date)) < today_date:
			return "Overdue"
		return "Pending"

	def _default_installment_label(self, idx):
		return "Advance" if idx == 1 else f"Installment {idx}"

	def _get_paid_dates_by_installment(self, invoice):
		thresholds = []
		running = 0.0
		for row in invoice.get("payment_schedule") or []:
			running += flt(row.payment_amount)
			thresholds.append(running)

		payment_events = []
		for advance in invoice.get("advances") or []:
			posting_date = self._get_payment_source_date(advance.reference_type, advance.reference_name)
			if posting_date and flt(advance.allocated_amount):
				payment_events.append({
					"posting_date": posting_date,
					"amount": flt(advance.allocated_amount),
					"name": advance.reference_name,
				})

		payment_events.extend(self._get_invoice_payment_entry_events(invoice.name))
		payment_events.sort(key=lambda d: (d["posting_date"], d["name"]))

		paid_dates = {}
		cumulative_paid = 0.0
		threshold_idx = 0
		for event in payment_events:
			cumulative_paid += flt(event["amount"])
			while threshold_idx < len(thresholds) and cumulative_paid >= thresholds[threshold_idx]:
				paid_dates[threshold_idx + 1] = event["posting_date"]
				threshold_idx += 1

		return paid_dates

	def _get_payment_source_date(self, reference_type, reference_name):
		if not reference_type or not reference_name or not frappe.db.exists(reference_type, reference_name):
			return None
		if reference_type == "Payment Entry":
			return frappe.db.get_value("Payment Entry", reference_name, "posting_date")
		if reference_type == "Journal Entry":
			return frappe.db.get_value("Journal Entry", reference_name, "posting_date")
		return None

	def _get_invoice_payment_entry_events(self, invoice_name):
		return frappe.db.sql(
			"""
			select
				pe.posting_date,
				per.allocated_amount as amount,
				pe.name
			from `tabPayment Entry` pe
			inner join `tabPayment Entry Reference` per
				on per.parent = pe.name
			where pe.docstatus = 1
			  and per.reference_doctype = 'Sales Invoice'
			  and per.reference_name = %s
			order by pe.posting_date asc, pe.name asc
			""",
			(invoice_name,),
			as_dict=True,
		)

	# ------------------------------------------------------------------ #
	#  GL Entry helpers                                                    #
	# ------------------------------------------------------------------ #

	def _post_completion_entries(self, settings):
		if self.government_fee_entry:
			return None

		selling_price = flt(self.selling_price)
		govt_fee = flt(self.government_fee_withheld)
		net_revenue = selling_price - govt_fee

		if selling_price <= 0:
			return None

		accounts = [{
			"account": settings.customer_advance_account,
			"debit_in_account_currency": selling_price,
			"party_type": "Customer",
			"party": self.customer,
		}]

		if govt_fee > 0:
			accounts.append({
				"account": settings.government_payable_account,
				"credit_in_account_currency": govt_fee,
			})

		accounts.append({
			"account": settings.revenue_account,
			"credit_in_account_currency": net_revenue,
		})

		je = frappe.get_doc({
			"doctype": "Journal Entry",
			"posting_date": today(),
			"company": settings.company,
			"voucher_type": "Journal Entry",
			"user_remark": (
				f"Revenue recognition — Contract {self.name}, Plot {self.plot}, "
				f"Customer {self.customer}"
			),
			"accounts": accounts,
		})
		je.insert(ignore_permissions=True)
		je.submit()
		self.db_set("government_fee_entry", je.name)
		return je.name

	def _post_termination_journal_entry(self, settings):
		if self.forfeiture_entry:
			return None

		total_paid = flt(self.total_paid)
		if total_paid <= 0:
			return None

		je = frappe.get_doc({
			"doctype": "Journal Entry",
			"posting_date": today(),
			"company": settings.company,
			"voucher_type": "Journal Entry",
			"user_remark": (
				f"Contract termination — funds forfeited (no refund). "
				f"Contract {self.name}, Plot {self.plot}, Customer {self.customer}"
			),
			"accounts": [
				{
					"account": settings.customer_advance_account,
					"debit_in_account_currency": total_paid,
					"party_type": "Customer",
					"party": self.customer,
				},
				{
					"account": settings.forfeited_deposits_account,
					"credit_in_account_currency": total_paid,
				},
			],
		})
		je.insert(ignore_permissions=True)
		je.submit()
		self.db_set("forfeiture_entry", je.name)
		return je.name

	# ------------------------------------------------------------------ #
	#  Contract termination                                                #
	# ------------------------------------------------------------------ #

	@frappe.whitelist()
	def terminate_contract(self, reason):
		if self.contract_status != "Ongoing":
			frappe.throw("Only Ongoing contracts can be terminated.")
		if self.docstatus != 1:
			frappe.throw("Document must be submitted before it can be terminated.")
		if not reason or not str(reason).strip():
			frappe.throw("A termination reason is required.")

		settings = frappe.get_single("LandMS Settings")
		self.reload()
		self._cancel_unpaid_plot_invoice()
		je_name = self._post_termination_journal_entry(settings)

		frappe.db.set_value("Plot Master", self.plot, "status", "Available")
		self._sync_land_acquisition_summary()
		self.db_set("contract_status", "Terminated")
		self.db_set("termination_reason", str(reason).strip())

		msg = f"Contract terminated. Plot {self.plot} is now Available for new contracts."
		if je_name:
			total_paid = flt(self.total_paid)
			msg += (
				f" TZS {total_paid:,.0f} paid by customer is forfeited (no refund) — "
				f"Journal Entry: {je_name}."
			)
		frappe.msgprint(msg, indicator="orange", alert=True)

	def _cancel_unpaid_plot_invoice(self):
		invoice_name = self._get_plot_invoice_name()
		if not invoice_name:
			return
		si_doc = frappe.get_doc("Sales Invoice", invoice_name)
		if si_doc.docstatus == 0:
			frappe.delete_doc("Sales Invoice", invoice_name, ignore_permissions=True)
			return
		if si_doc.docstatus == 1 and flt(si_doc.outstanding_amount) > 0:
			si_doc.cancel()


@frappe.whitelist()
def get_linked_documents_summary(plot_contract):
	if not plot_contract or not frappe.db.exists("Plot Contract", plot_contract):
		return ""

	doc = frappe.get_doc("Plot Contract", plot_contract)
	so_doc = doc._get_sales_order_doc()
	plot_application = doc.plot_application or (so_doc.get("plot_application") if so_doc else "")
	application_doc = frappe.get_doc("Plot Application", plot_application) if plot_application and frappe.db.exists("Plot Application", plot_application) else None

	rows = [
		("Sales Order", _doc_link("Sales Order", doc.sales_order)),
		("TCB Control Number", escape_html(doc.control_number or "-")),
		("Plot Sales Invoice", _doc_link("Sales Invoice", doc.booking_fee_invoice)),
	]

	if application_doc:
		rows.extend([
			("Plot Application", _doc_link("Plot Application", application_doc.name)),
			("Application Fee Invoice", _doc_link("Sales Invoice", application_doc.sales_invoice)),
			("Application Fee Payment Entry", _doc_link("Payment Entry", application_doc.payment_entry)),
		])

	if doc.government_fee_entry:
		rows.append(("Government Fee JE", _doc_link("Journal Entry", doc.government_fee_entry)))
	if doc.forfeiture_entry:
		rows.append(("Forfeiture JE", _doc_link("Journal Entry", doc.forfeiture_entry)))

	payment_entries = _get_plot_payment_entries(doc)
	payment_entries_html = "<span class='text-muted'>-</span>"
	if payment_entries:
		payment_entries_html = "<br>".join(
			_doc_link("Payment Entry", row.name) + f" <span class='text-muted'>({escape_html(str(row.posting_date))})</span>"
			for row in payment_entries
		)

	body = "".join(
		f"""
		<tr>
			<td style="padding:10px; font-weight:600; white-space:nowrap;">{escape_html(label)}</td>
			<td style="padding:10px;">{value or "<span class='text-muted'>-</span>"}</td>
		</tr>
		"""
		for label, value in rows
	)
	body += f"""
	<tr>
		<td style="padding:10px; font-weight:600; white-space:nowrap;">Payment Entries</td>
		<td style="padding:10px;">{payment_entries_html}</td>
	</tr>
	"""

	return f"""
	<div class="table-responsive">
		<table class="table table-bordered" style="margin-bottom:0; font-size:13px;">
			<tbody>{body}</tbody>
		</table>
	</div>
	"""


def _doc_link(doctype, name):
	if not name or not frappe.db.exists(doctype, name):
		return "<span class='text-muted'>-</span>"
	url = get_url_to_form(doctype, name)
	return f"<a href='{escape_html(url)}' target='_blank'>{escape_html(name)}</a>"


def _get_plot_payment_entries(doc):
	names = set()
	rows = []

	if doc.sales_order:
		so_rows = frappe.db.sql(
			"""
			select distinct pe.name, pe.posting_date
			from `tabPayment Entry` pe
			inner join `tabPayment Entry Reference` per
				on per.parent = pe.name
			where pe.docstatus = 1
			  and per.reference_doctype = 'Sales Order'
			  and per.reference_name = %s
			order by pe.posting_date asc, pe.name asc
			""",
			(doc.sales_order,),
			as_dict=True,
		)
		for row in so_rows:
			if row.name not in names:
				names.add(row.name)
				rows.append(row)

	if doc.booking_fee_invoice:
		si_rows = frappe.db.sql(
			"""
			select distinct pe.name, pe.posting_date
			from `tabPayment Entry` pe
			inner join `tabPayment Entry Reference` per
				on per.parent = pe.name
			where pe.docstatus = 1
			  and per.reference_doctype = 'Sales Invoice'
			  and per.reference_name = %s
			order by pe.posting_date asc, pe.name asc
			""",
			(doc.booking_fee_invoice,),
			as_dict=True,
		)
		for row in si_rows:
			if row.name not in names:
				names.add(row.name)
				rows.append(row)

	rows.sort(key=lambda d: (d.posting_date, d.name))
	return rows
