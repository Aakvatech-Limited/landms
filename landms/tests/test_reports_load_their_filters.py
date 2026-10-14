"""Every LandMS report's screen script registers under the report's own name.

Frappe looks the script up by the report name; a script registered under any
other name ("LMS ..." instead of "LandMS ...") is never used, so the report
opens without its filters and colours. Found on 6 reports, 14 Oct 2026.
"""

import os

import frappe
from frappe.tests.utils import FrappeTestCase

REPORT_DIR = frappe.get_app_path("landms", "landms", "report")


class TestReportsLoadTheirFilters(FrappeTestCase):
	def test_every_report_script_uses_its_report_name(self):
		checked = 0
		for folder in sorted(os.listdir(REPORT_DIR)):
			json_path = os.path.join(REPORT_DIR, folder, f"{folder}.json")
			js_path = os.path.join(REPORT_DIR, folder, f"{folder}.js")
			if not (os.path.exists(json_path) and os.path.exists(js_path)):
				continue
			name = frappe.get_file_json(json_path)["name"]
			with open(js_path) as f:
				script = f.read()
			with self.subTest(report=name):
				self.assertIn(f'frappe.query_reports["{name}"]', script)
			checked += 1
		self.assertGreater(checked, 0)
