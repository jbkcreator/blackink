"""The automatic net config/tenant_policies.py says it does not have.

That module's docstring states the residual risk plainly: "A table with
implicit (join-based) tenant scoping can't be reliably detected by column
introspection alone, so it must be registered here explicitly... there is
no automatic net for that."

That is true of the REGISTRY (nothing can tell you a table you forgot to
register should have been). It is NOT true of the two lists that must stay
in sync WITH the registry once a table is in it, and both have already
drifted: agent_work_orders was registered in TENANT_POLICIES but its
migration was never added to the CI workflow, so apply_rls_policies.py —
which iterates the registry and ALTERs every table in it — died on
`relation "agent_work_orders" does not exist`. The failure surfaced as a
schema error in a nightly security job, several steps away from the actual
mistake (a stale list in a YAML file).

These are pure text checks over the repo: no database, no Slack, no
network. They catch the drift at the point it is introduced.
"""

import re
from pathlib import Path

import pytest

from config.tenant_policies import TENANT_POLICIES

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "tenant_leakage_nightly.yml"

# The migration that must run last; it is the consumer of TENANT_POLICIES.
RLS_MIGRATION = "apply_rls_policies.py"


def _migration_creating(table: str) -> Path | None:
	"""The migration file whose CREATE TABLE brings `table` into existence."""
	pattern = re.compile(rf"CREATE TABLE IF NOT EXISTS\s+{re.escape(table)}\b")
	for path in sorted(MIGRATIONS_DIR.glob("apply_*.py")):
		if pattern.search(path.read_text(encoding="utf-8")):
			return path
	return None


@pytest.mark.parametrize("table", sorted(TENANT_POLICIES))
def test_every_tenant_table_has_a_migration_that_creates_it(table):
	assert _migration_creating(table) is not None, (
		f"{table!r} is registered in config/tenant_policies.py but no migration "
		f"CREATEs it. apply_rls_policies.py will fail with UndefinedTable."
	)


@pytest.mark.parametrize("table", sorted(TENANT_POLICIES))
def test_every_tenant_table_migration_runs_in_ci(table):
	"""The exact drift that broke the build: registered in the policy file,
	created by a migration, but that migration never runs in CI."""
	migration = _migration_creating(table)
	assert migration is not None, f"no migration creates {table!r}"
	workflow = WORKFLOW.read_text(encoding="utf-8")
	assert f"migrations/{migration.name}" in workflow, (
		f"{table!r} is tenant-scoped and created by migrations/{migration.name}, "
		f"but that migration is not run in {WORKFLOW.name}. apply_rls_policies.py "
		f"iterates TENANT_POLICIES and will abort on the missing table."
	)


@pytest.mark.parametrize("table", sorted(TENANT_POLICIES))
def test_tenant_table_migrations_run_before_rls_in_ci(table):
	"""Ordering, not just presence — apply_rls_policies.py is documented to
	run LAST because it ALTERs tables the earlier migrations create."""
	migration = _migration_creating(table)
	assert migration is not None, f"no migration creates {table!r}"
	workflow = WORKFLOW.read_text(encoding="utf-8")
	create_at = workflow.index(f"migrations/{migration.name}")
	rls_at = workflow.index(f"migrations/{RLS_MIGRATION}")
	assert create_at < rls_at, (
		f"migrations/{migration.name} runs AFTER {RLS_MIGRATION} in "
		f"{WORKFLOW.name}; it creates the tenant-scoped table {table!r}, so it "
		f"must run before RLS is applied."
	)


def test_rls_migration_is_actually_run_in_ci():
	"""Guards the guard: every check above is vacuous if the RLS step itself
	were dropped from the workflow."""
	assert f"migrations/{RLS_MIGRATION}" in WORKFLOW.read_text(encoding="utf-8")


def test_documented_migration_order_matches_ci():
	"""CLAUDE.md's migration block is the human-facing runbook and the
	workflow is the machine-facing one. A migration present in the runbook
	but absent from CI is untested; the reverse is undocumented."""
	claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
	workflow = WORKFLOW.read_text(encoding="utf-8")

	documented = set(re.findall(r"migrations/(apply_\w+\.py)", claude_md))
	in_ci = set(re.findall(r"migrations/(apply_\w+\.py)", workflow))

	# apply_akrash_grant.py is deliberately runbook-only: it grants INSERT to
	# the akrash_ingest role, and no leakage test exercises that path (the
	# function-privilege assertions in test_tenant_isolation.py come from
	# apply_compliance_gate_audit.py). Listed here so the exemption is a
	# decision on the record rather than a silent gap.
	runbook_only = {"apply_akrash_grant.py"}

	assert documented - in_ci - runbook_only == set(), (
		"documented in CLAUDE.md but never run in CI: "
		f"{sorted(documented - in_ci - runbook_only)}"
	)
	assert in_ci - documented == set(), (
		f"run in CI but undocumented in CLAUDE.md: {sorted(in_ci - documented)}"
	)
