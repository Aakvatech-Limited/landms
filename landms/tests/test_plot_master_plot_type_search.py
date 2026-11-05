"""The Plot Type dropdown on a plot lists its project's priced plot types (PM-4).

PM-4 replaced the raw SQL behind get_plot_types_for_la with the query builder.
The behaviour must not change, so tests 1-4 pass on the old code and the new;
the last one holds the file to "no raw SQL".

Safety: every write is rolled back; a database commit or an outside call fails
the test instead of happening. Uses plot types that already exist on the site.
"""

import inspect
import re
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import today

from landms.landms.doctype.plot_master import plot_master
from landms.landms.doctype.plot_master.plot_master import get_plot_types_for_la

# The rule of the CI check "Reject raw database SQL" (.github/workflows/no-raw-sql.yml), escaped so that
# the check itself does not count this line.
_RAW_SQL = re.compile(r"frappe\.db\.(sql|multisql)\b|(^|[^\w])db\.sql\s*\(", re.M)


def _no_commit():
	raise AssertionError(
		"a database commit happened inside the plot type test; nothing may be saved for real"
	)


def _no_outside_call(*args, **kwargs):
	raise AssertionError("an outside call was attempted inside the plot type test")


class TestPlotTypeSearch(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		frappe.db.before_commit.add(_no_commit)
		blocker = patch("requests.sessions.Session.request", side_effect=_no_outside_call)
		blocker.start()
		self.addCleanup(blocker.stop)

		existing = frappe.get_all("Plot Type", pluck="name", order_by="name")
		if len(existing) < 4:
			self.skipTest("this site needs at least 4 plot types")
		self.types = existing[:3]  # this project's types
		self.other_type = existing[3]  # priced only on the other project
		self.project = self._project(self.types)
		self.other_project = self._project([self.other_type])

	def _project(self, plot_types):
		company = frappe.db.get_value("Land Acquisition", {"docstatus": 1}, "company") or frappe.db.get_value(
			"Company", {}, "name"
		)
		return (
			frappe.get_doc(
				{
					"doctype": "Land Acquisition",
					"acquisition_name": "PM-4 test " + frappe.generate_hash(length=6),
					"acquisition_date": today(),
					"company": company,
					"region": "Dodoma",
					"district": "Test",
					"total_area_sqm": 1000,
					"land_use_type": "Residential",
					"booking_fee_percent": 60,
					"government_share_percent": 28.5,
					"payment_completion_days": 90,
					"collect_via_tcb": 1,
					"payment_channel": "TCB",
					"partner_code": "PART-P" + frappe.generate_hash(length=3).upper(),
					"plot_type_rates": [{"plot_type": t, "selling_price_per_sqm": 1000} for t in plot_types],
				}
			)
			.insert()
			.name
		)

	def _search(self, txt="", start=0, page_len=20, project=None):
		filters = {"land_acquisition": project or self.project}
		return [row[0] for row in get_plot_types_for_la("Plot Type", txt, "name", start, page_len, filters)]

	def test_lists_only_the_projects_plot_types_in_order(self):
		self.assertEqual(self._search(), sorted(self.types))
		self.assertNotIn(self.other_type, self._search())
		self.assertEqual(self._search(project=self.other_project), [self.other_type])

	def test_a_search_word_narrows_the_list(self):
		word = self.types[1]
		expected = [t for t in sorted(self.types) if word.lower() in t.lower()]
		self.assertEqual(self._search(txt=word), expected)
		self.assertEqual(self._search(txt="no plot type is called this"), [])

	def test_pages(self):
		ordered = sorted(self.types)
		self.assertEqual(self._search(start=0, page_len=1), ordered[:1])
		self.assertEqual(self._search(start=1, page_len=1), ordered[1:2])
		self.assertEqual(self._search(start=1, page_len=5), ordered[1:])

	def test_no_project_gives_an_empty_list(self):
		self.assertEqual(get_plot_types_for_la("Plot Type", "", "name", 0, 20, {}), [])

	def test_plot_master_code_has_no_raw_sql(self):
		self.assertIsNone(_RAW_SQL.search(inspect.getsource(plot_master)))
