app_name = "landms"
app_title = "Landms"
app_publisher = "\'Aakvatech Limited\'"
app_description = "\'Land Management System\'"
app_email = "info@aakvatech.com"
app_license = "mit"

required_apps = ["erpnext"]

after_install = "landms.install.after_install"

fixtures = [
	{"dt": "Role", "filters": [["name", "in", ["Land Acquisition Approver"]]]},
	{"dt": "Custom Field", "filters": [["module", "=", "LandMS"]]},
]

# Document Events — wired phase by phase as each module is built
# doc_events = {}

# Scheduled Tasks — wired in Phase 8
# scheduler_events = {}
