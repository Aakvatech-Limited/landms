import frappe
from frappe.model.document import Document
from frappe.utils import add_days, cint, flt, getdate, today

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
		# Cancel is reserved for contracts with no confirmed payment.
		# If money has been received, terminate_contract() must be used so the
		# forfeiture journal entry is posted.
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
		# Skip when contract is auto-created from a Sales Order — the SO already
		# validated the plot/application flow.
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
		"""Contracts linked to a Sales Order can only be activated after the
		first payment is recorded against the SO's plot Sales Invoice.
		"""
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
		"""True when the linked plot Sales Invoice has been partially/fully paid."""
		plot_invoice = so_doc.get("plot_sales_invoice")
		if not plot_invoice or not frappe.db.exists("Sales Invoice", plot_invoice):
			return False
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
			"Plot Master", self.plot,
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
				"Land Acquisition", plot_data.land_acquisition, "acquisition_name"
			) or ""

	def calculate_financials(self):
		if flt(self.selling_price) > 0 and flt(self.booking_fee_percent) > 0:
			self.booking_fee_amount = flt(self.selling_price) * flt(self.booking_fee_percent) / 100
			self.balance_due = flt(self.selling_price) - flt(self.booking_fee_amount)
		if self.contract_date and flt(self.payment_completion_days) > 0:
			self.payment_deadline = add_days(self.contract_date, int(self.payment_completion_days))

	def generate_payment_schedule(self):
		"""Rebuild schedule rows from contract parameters.

		Only runs while the document is in Draft. Once submitted, rows are
		updated by sync_payment_status() after each payment.

		Skipped when contract is auto-created from a Sales Order — the schedule
		is set directly by the SO hook.
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

		self.payment_schedule = []

		# Row 1 — booking fee due on contract date
		self.append("payment_schedule", {
			"installment_number": 1,
			"due_date": self.contract_date,
			"expected_amount": booking_fee,
			"paid_amount": 0,
			"status": "Pending",
		})

		# Row 2 — remaining balance due on contract date + total days
		if balance > 0:
			self.append("payment_schedule", {
				"installment_number": 2,
				"due_date": add_days(self.contract_date, total_days),
				"expected_amount": balance,
				"paid_amount": 0,
				"status": "Pending",
			})

	def calculate_payment_summary(self):
		self.total_contract_value = flt(self.selling_price)
		total_paid = sum(flt(row.paid_amount) for row in self.payment_schedule)
		self.total_paid = total_paid
		self.total_outstanding = flt(self.selling_price) - total_paid
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
			"Plot Master", self.plot, "land_acquisition"
		)
		if la:
			sync_land_acquisition_plot_summary(la)

	# ------------------------------------------------------------------ #
	#  Sales Order link helpers                                            #
	# ------------------------------------------------------------------ #

	def _get_sales_order_doc(self):
		if not self.sales_order or not frappe.db.exists("Sales Order", self.sales_order):
			return None
		return frappe.get_doc("Sales Order", self.sales_order)

	def _get_plot_invoice_name(self):
		so_doc = self._get_sales_order_doc()
		if not so_doc:
			return ""
		invoice_name = so_doc.get("plot_sales_invoice") or ""
		if invoice_name and frappe.db.exists("Sales Invoice", invoice_name):
			return invoice_name
		return ""

	# ------------------------------------------------------------------ #
	#  Payment sync (single-SI model)                                      #
	# ------------------------------------------------------------------ #

	def sync_payment_status(self):
		"""Refresh contract totals + status from the single plot Sales Invoice
		owned by the linked Sales Order. Called by the payment_sync hooks
		after every Payment Entry submit/cancel.
		"""
		invoice_name = self._get_plot_invoice_name()
		if not invoice_name:
			return

		si = frappe.db.get_value(
			"Sales Invoice",
			invoice_name,
			["docstatus", "grand_total", "outstanding_amount"],
			as_dict=True,
		)
		if not si or si.docstatus != 1:
			return

		total_paid = max(0.0, flt(si.grand_total) - flt(si.outstanding_amount))
		total_outstanding = max(0.0, flt(si.outstanding_amount))

		# Auto-submit the draft contract on first payment.
		if total_paid > 0 and self.docstatus == 0:
			self.submit()
			self.reload()

		self._sync_schedule_rows(total_paid)
		self.db_set("total_paid", total_paid)
		self.db_set("total_outstanding", total_outstanding)
		self.db_set("payment_progress", self._derive_payment_progress(total_paid, total_outstanding))

		# Flip the linked Plot Application from Paid → Converted on first payment.
		so_doc = self._get_sales_order_doc()
		if so_doc and so_doc.get("plot_application"):
			app_status = frappe.db.get_value("Plot Application", so_doc.plot_application, "status")
			if total_paid > 0 and app_status == "Paid":
				frappe.db.set_value(
					"Plot Application", so_doc.plot_application, "status", "Converted"
				)

		if total_outstanding <= 0 and self.docstatus == 1:
			self.db_set("contract_status", "Completed")
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
			self.db_set("contract_status", "Ongoing")
			if frappe.db.get_value("Plot Master", self.plot, "status") == "Pending Advance":
				frappe.db.set_value("Plot Master", self.plot, "status", "Reserved")
				self._sync_land_acquisition_summary()

	def _sync_schedule_rows(self, total_paid):
		"""Distribute total_paid across the schedule rows in installment order."""
		now = getdate(today())
		remaining_paid = flt(total_paid)

		for row in sorted(self.payment_schedule, key=lambda d: cint(d.installment_number or 0)):
			expected = flt(row.expected_amount)
			paid_amount = min(expected, max(remaining_paid, 0.0))
			remaining_paid -= paid_amount

			if expected > 0 and paid_amount >= expected:
				status = "Paid"
			elif getdate(str(row.due_date)) < now:
				status = "Overdue"
			else:
				status = "Pending"

			row.paid_amount = paid_amount
			row.status = status
			frappe.db.set_value(
				"Plot Contract Payment",
				row.name,
				{"paid_amount": paid_amount, "status": status},
			)

	# ------------------------------------------------------------------ #
	#  GL Entry helpers                                                    #
	# ------------------------------------------------------------------ #

	def _post_completion_entries(self, settings):
		"""Recognise revenue and record the government fee on full payment.

		Idempotent — re-runs are no-ops once government_fee_entry is set.

		Accounting:
		  Dr Customer Advances     (selling_price — clears full liability)
		  Cr Government Payable    (government_fee_withheld)
		  Cr Plot Sales Revenue    (selling_price - government_fee_withheld)
		"""
		if self.government_fee_entry:
			return None

		selling_price = flt(self.selling_price)
		govt_fee      = flt(self.government_fee_withheld)
		net_revenue   = selling_price - govt_fee

		if selling_price <= 0:
			return None

		accounts = [
			{
				"account": settings.customer_advance_account,
				"debit_in_account_currency": selling_price,
				"party_type": "Customer",
				"party": self.customer,
			},
		]

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
			"doctype":      "Journal Entry",
			"posting_date": today(),
			"company":      settings.company,
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
		"""Forfeit ALL money received when a paid contract is terminated.

		The customer does not get a refund — the entire amount paid is recognised
		as forfeited deposits income.

		Accounting:
		  Dr Customer Advances        (total paid — reduce liability)
		  Cr Forfeited Deposits Income (total paid — recognise income)
		"""
		if self.forfeiture_entry:
			return None

		total_paid = flt(self.total_paid)
		if total_paid <= 0:
			return None

		je = frappe.get_doc({
			"doctype":      "Journal Entry",
			"posting_date": today(),
			"company":      settings.company,
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

		# Cancel the unpaid plot Sales Invoice if any outstanding remains.
		self._cancel_unpaid_plot_invoice()

		# Forfeit any money already received.
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
		"""Cancel the linked plot Sales Invoice if it still has outstanding balance.

		Paid invoices are left untouched — the forfeiture JE handles the books.
		"""
		invoice_name = self._get_plot_invoice_name()
		if not invoice_name:
			return
		si_doc = frappe.get_doc("Sales Invoice", invoice_name)
		if si_doc.docstatus == 0:
			frappe.delete_doc("Sales Invoice", invoice_name, ignore_permissions=True)
			return
		if si_doc.docstatus == 1 and flt(si_doc.outstanding_amount) > 0:
			si_doc.cancel()
