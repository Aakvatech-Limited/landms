"""Unit tests for landms.tcb — control number generator, validator, registry.

These cover the pure-logic surface that doesn't require Sales Order /
Customer fixtures. End-to-end SO submit / TCB outbound tests live in a
separate file (Phase 8 / integration suite).
"""

import re

import frappe
from frappe.tests.utils import FrappeTestCase

from landms import tcb
from landms.landms.doctype.tcb_control_number.tcb_control_number import (
	ALLOWED_TRANSITIONS,
)


DEFAULT_PATTERN = "99911####00##"


class TestControlNumberPattern(FrappeTestCase):
	"""Pattern compilation and validator behaviour."""

	def test_default_pattern_compiles_to_expected_regex(self):
		regex = tcb._pattern_to_regex(DEFAULT_PATTERN)
		# 99911 literal, 4 digits, 00 literal, 2 digits — 13 chars total.
		self.assertTrue(regex.match("9991143790087"))
		self.assertEqual(regex.pattern, r"^99911\d\d\d\d00\d\d$")

	def test_validator_accepts_valid_default(self):
		self.assertTrue(tcb.is_valid_control_number("9991143790087", pattern=DEFAULT_PATTERN))
		self.assertTrue(tcb.is_valid_control_number("9991100000000", pattern=DEFAULT_PATTERN))

	def test_validator_rejects_wrong_length(self):
		self.assertFalse(tcb.is_valid_control_number("999114379008", pattern=DEFAULT_PATTERN))   # 12
		self.assertFalse(tcb.is_valid_control_number("99911437900870", pattern=DEFAULT_PATTERN)) # 14

	def test_validator_rejects_wrong_prefix(self):
		self.assertFalse(tcb.is_valid_control_number("8881143790087", pattern=DEFAULT_PATTERN))

	def test_validator_rejects_wrong_middle_segment(self):
		# Position 10-11 must be '00' per default pattern.
		self.assertFalse(tcb.is_valid_control_number("9991143791187", pattern=DEFAULT_PATTERN))

	def test_validator_rejects_non_digits(self):
		self.assertFalse(tcb.is_valid_control_number("99911aa790087", pattern=DEFAULT_PATTERN))
		self.assertFalse(tcb.is_valid_control_number("9991143-790087", pattern=DEFAULT_PATTERN))

	def test_validator_rejects_empty(self):
		self.assertFalse(tcb.is_valid_control_number("", pattern=DEFAULT_PATTERN))
		self.assertFalse(tcb.is_valid_control_number(None, pattern=DEFAULT_PATTERN))

	def test_validator_with_custom_pattern(self):
		self.assertTrue(tcb.is_valid_control_number("88812", pattern="888##"))
		self.assertFalse(tcb.is_valid_control_number("8881", pattern="888##"))
		self.assertFalse(tcb.is_valid_control_number("888XX", pattern="888##"))


class TestControlNumberGenerator(FrappeTestCase):
	"""Generator: shape, randomness, uniqueness."""

	def test_generator_produces_valid_default(self):
		for _ in range(10):
			cn = tcb.generate_control_number(pattern=DEFAULT_PATTERN)
			self.assertTrue(tcb.is_valid_control_number(cn, pattern=DEFAULT_PATTERN),
			                f"generated {cn} does not match default pattern")
			self.assertEqual(len(cn), 13)
			self.assertTrue(cn.startswith("99911"))
			self.assertEqual(cn[9:11], "00")

	def test_generator_unique_across_batch(self):
		batch = {tcb.generate_control_number(pattern=DEFAULT_PATTERN) for _ in range(50)}
		# 50 samples from a 6-digit random space (4 + 2 = 6 random digits = 1M
		# combinations); collisions are extremely unlikely.
		self.assertEqual(len(batch), 50)

	def test_generator_uses_secrets_not_random(self):
		"""Sanity check: positions that are '#' should vary across draws."""
		samples = [tcb.generate_control_number(pattern=DEFAULT_PATTERN) for _ in range(20)]
		positions = [5, 6, 7, 8, 11, 12]  # the 6 random positions
		for pos in positions:
			values = {s[pos] for s in samples}
			self.assertGreater(
				len(values), 1,
				f"position {pos} produced the same digit across 20 samples — generator is not random",
			)


class TestControlNumberRegistry(FrappeTestCase):
	"""TCB Control Number doctype lifecycle."""

	def test_allowed_transitions_table_is_consistent(self):
		# Sanity-check the allow-list: every state name appears as a key.
		states = {"Generated", "Registered", "Paid", "Declined", "Expired", "Failed"}
		self.assertEqual(set(ALLOWED_TRANSITIONS.keys()), states)
		# Terminal states should have empty allow sets.
		self.assertEqual(ALLOWED_TRANSITIONS["Paid"], set())
		self.assertEqual(ALLOWED_TRANSITIONS["Declined"], set())
		self.assertEqual(ALLOWED_TRANSITIONS["Expired"], set())

	def test_registry_uses_control_number_as_name(self):
		# Pull the doctype meta and check the autoname rule.
		meta = frappe.get_meta("TCB Control Number")
		self.assertEqual(meta.autoname, "field:control_number")


class TestSettingsAccess(FrappeTestCase):
	"""TCB Integration Settings is read defensively."""

	def test_safe_off_defaults_when_doc_unset(self):
		# Even on a fresh install, _get_tcb_settings should give a usable dict.
		settings = tcb._get_tcb_settings()
		self.assertIn("enabled", settings)
		self.assertIn("outbound_mode", settings)

	def test_register_in_off_mode_returns_ignored(self):
		# Default mode is Off; registering should be ignored, not throw.
		result = tcb.register_reference_for_sales_order("DUMMY-SO", "9991100000000")
		self.assertTrue(result["ok"])
		self.assertEqual(result["mode"], "Off")
		self.assertIn("skipped", result["message"].lower())

	def test_decline_with_no_control_number_is_noop(self):
		result = tcb.decline_reference_for_sales_order("DUMMY-SO", "")
		self.assertTrue(result["ok"])
		self.assertEqual(result["status"], "Ignored")



class TestPatternRegexEscape(FrappeTestCase):
	"""Defense: pattern characters that look like regex meta must not blow up."""

	def test_literal_dots_are_escaped(self):
		# A pattern with regex meta characters should still compile.
		# (Our settings validator only allows digits and '#', so this can
		# only happen if someone bypasses the UI — but the regex helper
		# should still be safe.)
		regex = tcb._pattern_to_regex("9.9##")
		self.assertTrue(regex.match("9.912"))
		self.assertFalse(regex.match("99912"))  # '.' is escaped, not wildcard
