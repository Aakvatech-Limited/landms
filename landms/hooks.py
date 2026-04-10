app_name = "landms"
app_title = "Landms"
app_publisher = "Aakvatech Limited"
app_description = "Land Management System"
app_email = "info@aakvatech.com"
app_license = "mit"

required_apps = ["erpnext"]

after_install = "landms.install.after_install"

fixtures = [
	{"dt": "Role", "filters": [["name", "in", ["Land Acquisition Approver"]]]},
	{"dt": "Custom Field", "filters": [["module", "=", "LandMS"]]},
	{"dt": "Client Script", "filters": [["module", "=", "LandMS"]]},
	{"dt": "Workflow", "filters": [["document_type", "=", "Land Acquisition"]]},
	{"dt": "Dashboard Chart", "filters": [["module", "=", "LandMS"]]},
	{"dt": "Number Card", "filters": [["module", "=", "LandMS"]]},
	{"dt": "Workspace", "filters": [["module", "=", "LandMS"]]},
]

doc_events = {
	"Purchase Order": {
		"on_submit":              "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_order",
		"on_cancel":              "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_order",
		"on_update_after_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_order",
	},
	"Purchase Invoice": {
		"on_submit":              "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_invoice",
		"on_cancel":              "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_invoice",
		"on_update_after_submit": "landms.landms.doctype.land_acquisition.land_acquisition.sync_costs_from_purchase_invoice",
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
	"Sales Order": {
		"validate":  "landms.sales_order_hooks.validate_sales_order",
		"on_submit": "landms.sales_order_hooks.submit_sales_order",
		"on_cancel": "landms.sales_order_hooks.cancel_sales_order",
	},
}

scheduler_events = {
	"daily": [
		"landms.tasks.daily",
	],
}
