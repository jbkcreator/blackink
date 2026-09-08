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
from src.services.clients import provision_client

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "services" / "billing"
REPO_SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

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


def test_provision_client_requires_founding_with_no_default():
	"""PR #37 review finding — 'founding clients are not marked
	automatically'. provision_client() is the one write path for creating a
	clients row; `founding` having NO default means a new call site is
	forced to make an explicit decision rather than silently defaulting a
	real September founding client to False."""
	sig = inspect.signature(provision_client)
	founding_param = sig.parameters["founding"]
	assert founding_param.default is inspect.Parameter.empty, (
		"provision_client()'s founding parameter must have no default — "
		"a caller must always state it explicitly"
	)


def test_no_other_client_insert_in_production_src():
	"""provision_client() (src/services/clients.py) must be the ONLY
	production write path that INSERTs a clients row. src/tasks/ dev-seed
	scripts are deliberately excluded — seed_demo_sandbox.py is sandbox/demo
	data, not real account provisioning."""
	allowed = {
		REPO_SRC_ROOT / "services" / "clients.py",
		REPO_SRC_ROOT / "tasks" / "seed_demo_sandbox.py",
	}
	offenders = []
	for path in REPO_SRC_ROOT.rglob("*.py"):
		if "__pycache__" in path.parts or path in allowed:
			continue
		text = path.read_text(encoding="utf-8", errors="replace")
		if re.search(r"INSERT\s+INTO\s+clients\b", text, re.IGNORECASE):
			offenders.append(str(path))
	assert offenders == [], f"a clients row is INSERTed outside provision_client(): {offenders}"
