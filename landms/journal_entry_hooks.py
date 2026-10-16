"""Journal Entry doc-event hooks for LandMS.

The cost-centre rule now lives in landms.cost_center (one rule for every
LandMS doctype, wired as before_validate in hooks.py). This name is kept so
any external wiring that still points at it keeps working.
"""

from landms.cost_center import apply_landms_cost_center


def before_save_journal_entry(doc, method=None):
	apply_landms_cost_center(doc, method)
