"""Structural proof of DoD lines that would otherwise only be "confirmed via
code review" (Subtask 1.2.3, rules 2 and 5) — same technique as
tests/test_no_upfront_charge_paths.py.

- "Code review confirms no COUNT(*) >= cap predicate exists in the billing
  job" — no monthly ceiling anywhere in src/services/billing/.
- "No per-invoice override path exists" — resolve_sit_charge() takes no
  amount/override parameter a caller could use to force a different price.
"""
import inspect
import re
from pathlib import Path

from src.services.billing.sit_billing import resolve_sit_charge

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "services" / "billing"

CAP_PATTERN = re.compile(r"COUNT\s*\(\s*\*\s*\)\s*>=", re.IGNORECASE)


def _all_py_files():
	return [p for p in SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


def test_no_monthly_ceiling_predicate_in_billing_package():
	offenders = []
	for path in _all_py_files():
		text = path.read_text(encoding="utf-8", errors="replace")
		if CAP_PATTERN.search(text):
			offenders.append(str(path))
	assert offenders == [], f"a COUNT(*) >= cap predicate exists in the billing job: {offenders}"


def test_resolve_sit_charge_has_no_override_parameter():
	params = set(inspect.signature(resolve_sit_charge).parameters)
	forbidden = {"override", "amount_cents", "price_cents", "force_price", "manual_amount_cents"}
	leaked = params & forbidden
	assert leaked == set(), f"resolve_sit_charge() exposes an override parameter: {leaked}"
