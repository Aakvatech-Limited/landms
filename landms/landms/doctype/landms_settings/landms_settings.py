import frappe
from frappe.model.document import Document
from frappe.utils import cint, flt


REQUIRED_ACCOUNTS = {
	"land_under_development_account": "Asset",
	"plot_inventory_account": "Asset",
	"customer_advance_account": "Liability",
	"revenue_account": "Income",
	"cogs_account": "Expense",
	"tcb_bank_account": "Asset",
	"government_payable_account": "Liability",
	"forfeited_deposits_account": "Income",
	"seller_payable_account": "Liability",
	"application_fee_income_account": "Income",
}

BANK_CASH_ACCOUNTS = (
	"application_fee_receiving_account",
	"tcb_bank_account",
)


class LandMSSettings(Document):

	def validate(self):
		self._validate_cost_center()
		self._validate_accounts()
		self._validate_fee_settings()
		self._validate_no_account_overlap()

	def _validate_accounts(self):
		for field, expected_root_type in REQUIRED_ACCOUNTS.items():
			account = self.get(field)
			if not account:
				frappe.throw(f"{self.meta.get_label(field)} is required.")
			self._check_account_company(field, account)
			self._check_root_type(field, account, expected_root_type)

		for field in BANK_CASH_ACCOUNTS:
			account = self.get(field)
			if not account:
				continue
			account_type = frappe.db.get_value("Account", account, "account_type")
			if account_type not in ("Bank", "Cash"):
				frappe.throw(f"{self.meta.get_label(field)} must be a Bank or Cash account.")

	def _check_account_company(self, field, account):
		company = frappe.db.get_value("Account", account, "company")
		if company and company != self.company:
			frappe.throw(
				f"'{account}' in {self.meta.get_label(field)} "
				f"belongs to company '{company}', not '{self.company}'."
			)

	def _check_root_type(self, field, account, expected):
		root_type = frappe.db.get_value("Account", account, "root_type")
		if root_type and root_type != expected:
			frappe.throw(
				f"{self.meta.get_label(field)} must be a {expected} account (got {root_type})."
			)

	def _validate_cost_center(self):
		if not self.cost_center:
			frappe.throw("Cost Center is required.")
		company = frappe.db.get_value("Cost Center", self.cost_center, "company")
		if company and company != self.company:
			frappe.throw(
				f"Cost Center '{self.cost_center}' belongs to company '{company}', not '{self.company}'."
			)

	def _validate_fee_settings(self):
		if flt(self.application_fee_amount) < 0:
			frappe.throw("Application Fee Amount cannot be negative. Set 0 to waive the fee.")
		if cint(self.unpaid_application_expiry_days) < 1:
			frappe.throw("Unpaid Application Expiry Days must be at least 1.")
		if cint(self.application_fee_validity_days) < 1:
			frappe.throw("Application Fee Validity Days must be at least 1.")

	def _validate_no_account_overlap(self):
		if (
			self.customer_advance_account
			and self.revenue_account
			and self.customer_advance_account == self.revenue_account
		):
			frappe.throw(
				"Customer Advance Account and Revenue Account cannot be the same. "
				"Advances are deferred — revenue is recognised at contract completion only."
			)