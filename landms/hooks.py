app_name = "landms"
app_title = "Landms"
app_publisher = "Aakvatech Limited"
app_description = "Land Management System"
app_email = "info@aakvatech.com"
app_license = "mit"

required_apps = ["erpnext"]

doctype_js = {
	"Sales Order": "public/js/sales_order.js",
	"Purchase Order": "public/js/purchase_order.js",
	"Purchase Invoice": "public/js/purchase_invoice.js",
}

after_install = "landms.install.after_install"

after_migrate = [
	"landms.patches.custom_fields.create_custom_fields.execute",
	"landms.patches.property_setter.create_property_setters.execute",
]

doc_events = {
	"Purchase Order": {
		"validate": "landms.landms.doctype.land_acquisition.land_acquisition.set_land_acquisition_expense_account",
		"on_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_order",
		"on_cancel": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_order",
		"on_update_after_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_order",
	},
	"Purchase Invoice": {
		"validate": "landms.landms.doctype.land_acquisition.land_acquisition.set_land_acquisition_expense_account",
		"on_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_invoice",
		"on_cancel": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_invoice",
		"on_update_after_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_invoice",
	},
	"Journal Entry": {
		"before_save": "landms.journal_entry_hooks.before_save_journal_entry",
		"on_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_journal_entry",
		"on_cancel": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_journal_entry",
	},
	"Payment Entry": {
		"validate": [
			"landms.landms.doctype.land_acquisition.land_acquisition.autoset_land_acquisition_on_payment_entry",
			"landms.payment_sync.validate_payment_entry",
		],
		"on_submit": [
			"landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_payment_entry",
			"landms.payment_sync.on_submit_payment_entry",
		],
		"on_cancel": [
			"landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_payment_entry",
			"landms.payment_sync.on_cancel_payment_entry",
		],
	},
	"Sales Invoice": {
		"before_save": "landms.sales_invoice_hooks.before_save_sales_invoice",
	},
	"Sales Order": {
		"validate": "landms.sales_order_hooks.validate_sales_order",
		"before_submit": "landms.sales_order_hooks.before_submit_sales_order",
		"on_submit": "landms.sales_order_hooks.submit_sales_order",
		"before_cancel": "landms.sales_order_hooks.before_cancel_sales_order",
		"on_cancel": "landms.sales_order_hooks.cancel_sales_order",
		# ERPNext's Status > Re-open: refused on a terminated plot order
		"before_change": "landms.sales_order_hooks.before_change_sales_order",
	},
}

scheduler_events = {
	"cron": {
		# the nightly expiry / overdue run at 01:00, after the TCB reconciliation;
		# it runs in the long queue (25 min, not 5) and hands anything left to a
		# follow-up job before it would be cut off
		"0 1 * * *": ["landms.tasks.start_nightly_run"],
	},
	"daily_long": [
		"landms.tasks.run_daily_tcb_reconciliation",
	],
}
