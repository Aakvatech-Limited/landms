from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from landms.api import tcb as api_tcb

MODULE = "landms.api.tcb"


def _fake_request(headers=None, remote_addr="10.0.0.1"):
	request = MagicMock()
	request.headers.get.side_effect = lambda name, default=None: (headers or {}).get(name, default)
	request.remote_addr = remote_addr
	return request


class TestGetClientIp(IntegrationTestCase):
	def test_reads_leftmost_x_forwarded_for_entry(self):
		request = _fake_request(headers={"X-Forwarded-For": "203.0.113.5, 10.0.0.1"})
		with patch.object(frappe.local, "request", request, create=True):
			self.assertEqual(api_tcb._get_client_ip(), "203.0.113.5")

	def test_falls_back_to_remote_addr_without_xff(self):
		request = _fake_request(headers={}, remote_addr="192.168.1.1")
		with patch.object(frappe.local, "request", request, create=True):
			self.assertEqual(api_tcb._get_client_ip(), "192.168.1.1")

	def test_returns_empty_string_outside_a_request_context(self):
		with patch.object(frappe.local, "request", None, create=True):
			self.assertEqual(api_tcb._get_client_ip(), "")


class TestIpnLock(IntegrationTestCase):
	def test_no_transaction_id_skips_locking(self):
		lock_name, acquired = api_tcb._acquire_ipn_lock("", "REF-1")
		self.assertEqual(lock_name, "")
		self.assertTrue(acquired)

	def test_acquires_named_lock_for_transaction(self):
		with patch(f"{MODULE}.frappe.db.sql", return_value=[[1]]) as mock_sql:
			lock_name, acquired = api_tcb._acquire_ipn_lock("TXN-1", "REF-1")
		self.assertTrue(acquired)
		self.assertIn("TXN-1", lock_name)
		mock_sql.assert_called_once()

	def test_fails_open_when_lock_already_held(self):
		with patch(f"{MODULE}.frappe.db.sql", return_value=[[0]]):
			_, acquired = api_tcb._acquire_ipn_lock("TXN-1", "REF-1")
		self.assertFalse(acquired)

	def test_lock_name_truncated_to_64_chars(self):
		long_ref = "R" * 100
		lock_name, _ = api_tcb._acquire_ipn_lock("TXN-1", long_ref)
		self.assertLessEqual(len(lock_name), 64)

	def test_release_is_a_noop_without_a_lock_name(self):
		with patch(f"{MODULE}.frappe.db.sql") as mock_sql:
			api_tcb._release_ipn_lock("")
		mock_sql.assert_not_called()


class TestParseHeaderNames(IntegrationTestCase):
	def test_defaults_to_authorization(self):
		self.assertEqual(api_tcb._parse_header_names(None), ["Authorization"])

	def test_splits_on_commas_and_newlines(self):
		names = api_tcb._parse_header_names("Authorization,\nX-Auth-Token")
		self.assertEqual(names, ["Authorization", "X-Auth-Token"])


class TestGetAllowedTcbIps(IntegrationTestCase):
	def test_parses_comma_and_newline_separated_list(self):
		with patch(f"{MODULE}.frappe.db.get_single_value", return_value="10.0.0.1,\n10.0.0.2"):
			self.assertEqual(api_tcb._get_allowed_tcb_ips(), ["10.0.0.1", "10.0.0.2"])

	def test_empty_setting_returns_empty_list(self):
		with patch(f"{MODULE}.frappe.db.get_single_value", return_value=""):
			self.assertEqual(api_tcb._get_allowed_tcb_ips(), [])

	def test_settings_read_error_fails_open_to_empty_list(self):
		with patch(f"{MODULE}.frappe.db.get_single_value", side_effect=Exception("no settings")):
			self.assertEqual(api_tcb._get_allowed_tcb_ips(), [])


class TestVerifyIpnAuthToken(IntegrationTestCase):
	def _settings(self, **kwargs):
		defaults = {
			"ipn_auth_mode": "Off",
			"ipn_auth_header": "Authorization",
		}
		defaults.update(kwargs)
		doc = frappe._dict(defaults)
		doc.get_password = MagicMock(return_value=kwargs.get("_token", ""))
		return doc

	def test_off_mode_passes_without_checking_headers(self):
		with patch(f"{MODULE}.frappe.get_cached_doc", return_value=self._settings(ipn_auth_mode="Off")):
			ok, mode, _ = api_tcb._verify_ipn_auth_token()
		self.assertTrue(ok)
		self.assertEqual(mode, "Off")

	def test_settings_read_failure_fails_open(self):
		with patch(f"{MODULE}.frappe.get_cached_doc", side_effect=Exception("boom")):
			ok, mode, _ = api_tcb._verify_ipn_auth_token()
		self.assertTrue(ok)
		self.assertEqual(mode, "Off")

	def test_enforce_without_configured_token_fails_closed(self):
		settings = self._settings(ipn_auth_mode="Enforce", _token="")
		with patch(f"{MODULE}.frappe.get_cached_doc", return_value=settings):
			ok, mode, _ = api_tcb._verify_ipn_auth_token()
		self.assertFalse(ok)
		self.assertEqual(mode, "Enforce")

	def test_log_only_without_configured_token_passes(self):
		settings = self._settings(ipn_auth_mode="Log Only", _token="")
		with patch(f"{MODULE}.frappe.get_cached_doc", return_value=settings):
			ok, _, _ = api_tcb._verify_ipn_auth_token()
		self.assertTrue(ok)

	def test_enforce_accepts_matching_token(self):
		settings = self._settings(ipn_auth_mode="Enforce", _token="secret-token")
		request = _fake_request(headers={"Authorization": "secret-token"})
		with (
			patch(f"{MODULE}.frappe.get_cached_doc", return_value=settings),
			patch.object(frappe.local, "request", request, create=True),
		):
			ok, mode, message = api_tcb._verify_ipn_auth_token()
		self.assertTrue(ok)
		self.assertIn("OK", message)

	def test_enforce_strips_bearer_prefix(self):
		settings = self._settings(ipn_auth_mode="Enforce", _token="secret-token")
		request = _fake_request(headers={"Authorization": "Bearer secret-token"})
		with (
			patch(f"{MODULE}.frappe.get_cached_doc", return_value=settings),
			patch.object(frappe.local, "request", request, create=True),
		):
			ok, _, _ = api_tcb._verify_ipn_auth_token()
		self.assertTrue(ok)

	def test_enforce_rejects_mismatched_token(self):
		settings = self._settings(ipn_auth_mode="Enforce", _token="secret-token")
		request = _fake_request(headers={"Authorization": "wrong-token"})
		with (
			patch(f"{MODULE}.frappe.get_cached_doc", return_value=settings),
			patch.object(frappe.local, "request", request, create=True),
		):
			ok, _, message = api_tcb._verify_ipn_auth_token()
		self.assertFalse(ok)
		self.assertIn("mismatch", message)

	def test_enforce_rejects_missing_header(self):
		settings = self._settings(ipn_auth_mode="Enforce", _token="secret-token")
		request = _fake_request(headers={})
		with (
			patch(f"{MODULE}.frappe.get_cached_doc", return_value=settings),
			patch.object(frappe.local, "request", request, create=True),
		):
			ok, _, message = api_tcb._verify_ipn_auth_token()
		self.assertFalse(ok)
		self.assertIn("missing", message)


class TestExtractIpnBody(IntegrationTestCase):
	def test_unwraps_param_key(self):
		envelope = {"status": 0, "param": {"reference": "REF-1"}}
		self.assertEqual(api_tcb._extract_ipn_body(envelope), {"reference": "REF-1"})

	def test_falls_back_to_flat_envelope_without_param(self):
		envelope = {"reference": "REF-1"}
		self.assertEqual(api_tcb._extract_ipn_body(envelope), envelope)

	def test_non_dict_envelope_returns_empty_dict(self):
		self.assertEqual(api_tcb._extract_ipn_body("not a dict"), {})


class TestSafeInt(IntegrationTestCase):
	def test_converts_numeric_string(self):
		self.assertEqual(api_tcb._safe_int("42"), 42)

	def test_none_stays_none(self):
		self.assertIsNone(api_tcb._safe_int(None))

	def test_unparseable_value_never_raises(self):
		# frappe.utils.cint swallows bad input to 0 rather than raising, so
		# _safe_int's own except branch is a defensive backstop, not reachable
		# through cint for a plain object() — the contract that matters is
		# "never raises", not the exact fallback value.
		self.assertEqual(api_tcb._safe_int(object()), 0)


class TestParseTcbDatetime(IntegrationTestCase):
	def test_strips_timezone_offset(self):
		result = api_tcb._parse_tcb_datetime("2020-12-29T15:05:21.000+0300")
		self.assertEqual(result, "2020-12-29 15:05:21")

	def test_empty_value_returns_none(self):
		self.assertIsNone(api_tcb._parse_tcb_datetime(""))

	def test_unparseable_value_falls_back_to_truncated_text(self):
		result = api_tcb._parse_tcb_datetime("not-a-date-at-all")
		self.assertIsInstance(result, str)


class TestToText(IntegrationTestCase):
	def test_none_becomes_empty_string(self):
		self.assertEqual(api_tcb._to_text(None), "")

	def test_string_passes_through(self):
		self.assertEqual(api_tcb._to_text("raw body"), "raw body")

	def test_dict_is_pretty_printed_json(self):
		result = api_tcb._to_text({"a": 1})
		self.assertIn('"a": 1', result)
