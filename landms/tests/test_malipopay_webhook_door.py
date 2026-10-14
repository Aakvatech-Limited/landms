"""The public Malipopay webhook keeps its door safe.

Deploy-day review, 14 Oct 2026: while Malipopay is switched off nothing is
stored (anyone can post to a public endpoint); an unsigned delivery keeps only
an excerpt of its body; a signed delivery replaces an unsigned row of the same
transaction instead of being dropped as a duplicate. Every write is rolled back.
"""

import hashlib
import hmac
import inspect
import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

from landms.api import malipopay as door

SECRET = "test-signing-secret"


def _post(body, signed):
	raw = json.dumps(body).encode()
	headers = {"Content-Type": "application/json"}
	if signed:
		headers[door.SIGNATURE_HEADER] = (
			"sha256=" + hmac.new(SECRET.encode(), raw, hashlib.sha256).hexdigest()
		)
	frappe.local.request = Request(EnvironBuilder(method="POST", data=raw, headers=headers).get_environ())
	frappe.local.response = frappe._dict()
	return inspect.unwrap(door.webhook)()  # past the whitelist and rate-limit wrappers


class TestMalipopayWebhookDoor(FrappeTestCase):
	def setUp(self):
		self.addCleanup(frappe.db.rollback)
		for target in ("frappe.db.commit",):
			p = patch(target)
			p.start()
			self.addCleanup(p.stop)
		p = patch("landms.api.malipopay._log")
		p.start()
		self.addCleanup(p.stop)
		p = patch("landms.api.malipopay._resolve_collection", return_value=None)
		p.start()
		self.addCleanup(p.stop)
		p = patch("landms.api.malipopay.frappe.enqueue")
		self.enqueue = p.start()
		self.addCleanup(p.stop)

	def _settings(self, enabled=1, mode="Apply Payment"):
		p = patch(
			"landms.api.malipopay.get_malipopay_settings",
			return_value=frappe._dict(enabled=enabled, inbound_mode=mode),
		)
		p.start()
		self.addCleanup(p.stop)

	def _secret(self, value=SECRET):
		p = patch("landms.api.malipopay._signing_secret", return_value=value)
		p.start()
		self.addCleanup(p.stop)

	def _event(self, txn):
		return frappe.db.get_value(
			"Malipopay Webhook Event",
			{"transaction_id": txn},
			["name", "hmac_verified", "processing_status", "raw_payload"],
			as_dict=True,
		)

	def test_switched_off_still_records_the_notice_as_a_short_excerpt(self):
		# a bill may still be live at Malipopay and it never resends
		self._settings(enabled=0)
		self._secret()
		body = {"transactionId": "TXN-DOOR-OFF", "event": "payment.confirmed", "pad": "x" * 20000}
		result = _post(body, signed=True)
		row = self._event("TXN-DOOR-OFF")
		self.assertIsNotNone(row)
		self.assertEqual(row.processing_status, "Held")
		self.assertLessEqual(len(row.raw_payload or ""), door.UNVERIFIED_BODY_BYTES)
		self.enqueue.assert_not_called()
		door._log.assert_not_called()
		self.assertEqual(result["status"], "Held")

	def test_an_unsigned_delivery_keeps_only_an_excerpt(self):
		self._settings()
		self._secret()
		_post(
			{"transactionId": "TXN-DOOR-BIG", "event": "payment.confirmed", "pad": "x" * 20000}, signed=False
		)
		row = self._event("TXN-DOOR-BIG")
		self.assertIsNotNone(row)
		self.assertEqual(row.hmac_verified, 0)
		self.assertLessEqual(len(row.raw_payload or ""), door.UNVERIFIED_BODY_BYTES)

	def test_a_signed_delivery_replaces_an_unsigned_one(self):
		self._settings()
		self._secret()
		body = {"transactionId": "TXN-DOOR-FORGED", "event": "payment.confirmed", "amount": 1000}
		_post(body, signed=False)
		self.assertEqual(self._event("TXN-DOOR-FORGED").hmac_verified, 0)
		result = _post(body, signed=True)
		row = self._event("TXN-DOOR-FORGED")
		self.assertEqual(row.hmac_verified, 1)
		self.assertNotEqual(result["status"], "Duplicate")
		self.assertEqual(frappe.db.count("Malipopay Webhook Event", {"transaction_id": "TXN-DOOR-FORGED"}), 1)

	def test_a_second_signed_delivery_is_still_a_duplicate(self):
		self._settings()
		self._secret()
		body = {"transactionId": "TXN-DOOR-TWICE", "event": "payment.confirmed", "amount": 1000}
		_post(body, signed=True)
		result = _post(body, signed=True)
		self.assertEqual(result["status"], "Duplicate")
