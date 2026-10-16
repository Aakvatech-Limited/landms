"""Malipopay's timed jobs run where they cannot be cut off half-way.

A timed (cron/hourly) job runs in the default queue, which stops it after five
minutes; a sweep cut off half-way leaves its window open and repeats the next
day. The hourly jobs use hourly_long; the 04:40 sweep and the 05:10 pass are
handed to the long queue by small starters.
"""

from unittest.mock import patch

from frappe.tests.utils import FrappeTestCase

from landms import hooks, malipopay_external, malipopay_sweep


class TestMalipopaySchedule(FrappeTestCase):
	def test_the_hourly_malipopay_jobs_run_in_the_long_queue(self):
		events = hooks.scheduler_events
		for job in (
			"landms.malipopay.process_pending_webhook_events",
			"landms.malipopay.kill_pending_collections",
		):
			self.assertIn(job, events["hourly_long"])
			self.assertNotIn(job, events.get("hourly", []))

	def test_the_morning_jobs_are_started_through_the_long_queue(self):
		cron = hooks.scheduler_events["cron"]
		self.assertEqual(cron["40 4 * * *"], ["landms.malipopay_sweep.start_daily_malipopay_sweep"])
		self.assertEqual(cron["10 5 * * *"], ["landms.malipopay_external.start_external_entries_sync"])
		self.assertEqual(cron["0 1 * * *"], ["landms.tasks.start_nightly_run"])

	def test_the_sweep_starter_hands_over_once(self):
		with patch("landms.malipopay_sweep.frappe.enqueue") as enqueue:
			malipopay_sweep.start_daily_malipopay_sweep()
		self.assertEqual(enqueue.call_args.args[0], "landms.malipopay_sweep.run_daily_malipopay_sweep")
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])

	def test_the_external_pass_starter_hands_over_once(self):
		with patch("landms.malipopay_external.frappe.enqueue") as enqueue:
			malipopay_external.start_external_entries_sync()
		self.assertEqual(enqueue.call_args.args[0], "landms.malipopay_external.sync_all_external_entries")
		self.assertEqual(enqueue.call_args.kwargs["queue"], "long")
		self.assertTrue(enqueue.call_args.kwargs["deduplicate"])
