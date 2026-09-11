"""The automatic net config/tenant_policies.py says it does not have.

That module's docstring states the residual risk plainly: "A table with
implicit (join-based) tenant scoping can't be reliably detected by column
introspection alone, so it must be registered here explicitly... there is
no automatic net for that."

That is true of the REGISTRY (nothing can tell you a table you forgot to
register should have been). It is NOT true of the lists that must stay in
sync WITH the registry once a table is in it, and this has now drifted
TWICE, in two different ways:

1. agent_work_orders was registered in TENANT_POLICIES but its migration
   was never added to the CI workflow, so apply_rls_policies.py — which
   iterates the registry and ALTERs every table in it — died on
   `relation "agent_work_orders" does not exist`.
2. winback_imports/winback_rows (Subtask 3.1.1) were added to
   tenant_leakage_nightly.yml but NOT to tests.yml — a second workflow
   that runs the exact same migration sequence for the main PR-gating test
   suite. Both times the failure surfaced as a schema error several steps
   away from the actual mistake (a stale list in a YAML file), and both
   times a local test run against only one workflow file passed while the
   other one's CI run failed.

Checking every migration-running workflow (not just one of them) is what
closes case 2 — a table registered and correctly wired into one workflow
but not the other now fails loudly here instead of on the next PR's CI run.

These are pure text checks over the repo: no database, no Slack, no
network. They catch the drift at the point it is introduced.
"""

import re
from pathlib import Path

import pytest

from config.tenant_policies import TENANT_POLICIES

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

# Every workflow file that actually runs the migration sequence (i.e.
# contains apply_rls_policies.py) — discovered, not hand-maintained, so a
# THIRD such workflow added later is picked up automatically instead of
# silently becoming a new unchecked copy of this same list.
WORKFLOWS = sorted(
	p for p in WORKFLOWS_DIR.glob("*.yml")
	if "migrations/apply_rls_policies.py" in p.read_text(encoding="utf-8")
)

# The migration that must run last; it is the consumer of TENANT_POLICIES.
RLS_MIGRATION = "apply_rls_policies.py"


def _migration_creating(table: str) -> Path | None:
	"""The migration file whose CREATE TABLE brings `table` into existence."""
	pattern = re.compile(rf"CREATE TABLE IF NOT EXISTS\s+{re.escape(table)}\b")
	for path in sorted(MIGRATIONS_DIR.glob("apply_*.py")):
		if pattern.search(path.read_text(encoding="utf-8")):
			return path
	return None


def test_at_least_one_migration_running_workflow_exists():
	"""Guards the discovery itself — if WORKFLOWS came back empty, every
	check below would vacuously pass and this file would protect nothing."""
	assert WORKFLOWS, (
		f"no *.yml under {WORKFLOWS_DIR} contains migrations/apply_rls_policies.py — "
		"either the workflow was renamed/removed, or migration-running CI is gone entirely."
	)


@pytest.mark.parametrize("table", sorted(TENANT_POLICIES))
def test_every_tenant_table_has_a_migration_that_creates_it(table):
	assert _migration_creating(table) is not None, (
		f"{table!r} is registered in config/tenant_policies.py but no migration "
		f"CREATEs it. apply_rls_policies.py will fail with UndefinedTable."
	)


@pytest.mark.parametrize("table", sorted(TENANT_POLICIES))
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_every_tenant_table_migration_runs_in_ci(table, workflow):
	"""The exact drift that broke the build (twice): registered in the
	policy file, created by a migration, but that migration never runs in
	one of the workflows that applies RLS. Checked against EVERY such
	workflow, not just one — that's what catches drift between them."""
	migration = _migration_creating(table)
	assert migration is not None, f"no migration creates {table!r}"
	contents = workflow.read_text(encoding="utf-8")
	assert f"migrations/{migration.name}" in contents, (
		f"{table!r} is tenant-scoped and created by migrations/{migration.name}, "
		f"but that migration is not run in {workflow.name}. apply_rls_policies.py "
		f"iterates TENANT_POLICIES and will abort on the missing table."
	)


@pytest.mark.parametrize("table", sorted(TENANT_POLICIES))
@pytest.mark.parametrize("workflow", WORKFLOWS, ids=lambda p: p.name)
def test_tenant_table_migrations_run_before_rls_in_ci(table, workflow):
	"""Ordering, not just presence — apply_rls_policies.py is documented to
	run LAST because it ALTERs tables the earlier migrations create."""
	migration = _migration_creating(table)
	assert migration is not None, f"no migration creates {table!r}"
	contents = workflow.read_text(encoding="utf-8")
	if f"migrations/{migration.name}" not in contents:
		pytest.skip("covered by test_every_tenant_table_migration_runs_in_ci's failure")
	create_at = contents.index(f"migrations/{migration.name}")
	rls_at = contents.index(f"migrations/{RLS_MIGRATION}")
	assert create_at < rls_at, (
		f"migrations/{migration.name} runs AFTER {RLS_MIGRATION} in "
		f"{workflow.name}; it creates the tenant-scoped table {table!r}, so it "
		f"must run before RLS is applied."
	)


def test_documented_migration_order_matches_ci():
	"""CLAUDE.md's migration block is the human-facing runbook and the
	workflows are the machine-facing ones. A migration present in the
	runbook but absent from EVERY migration-running workflow is untested;
	one present in a workflow but undocumented is a silent addition."""
	claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
	documented = set(re.findall(r"migrations/(apply_\w+\.py)", claude_md))
	in_any_ci: set[str] = set()
	for workflow in WORKFLOWS:
		in_any_ci |= set(re.findall(r"migrations/(apply_\w+\.py)", workflow.read_text(encoding="utf-8")))

	# apply_akrash_grant.py used to be exempted here as "deliberately
	# runbook-only, no leakage test exercises that path" — that exemption is
	# exactly what let a real bug ship silently (a PR review finding on
	# Subtask 3.1.1: a table-creation migration granted akrash_ingest access
	# directly, which this migration's own REVOKE ALL then wiped with no
	# error, since a later run had nothing "unexpected" left to warn about).
	# It now runs in every migration-running workflow, and
	# test_tenant_isolation.py's test_akrash_ingest_can_insert_but_not_
	# select_raw_assessor_parcels actually exercises the grant it produces —
	# no exemption needed.
	runbook_only: set[str] = set()

	assert documented - in_any_ci - runbook_only == set(), (
		"documented in CLAUDE.md but never run in any CI workflow: "
		f"{sorted(documented - in_any_ci - runbook_only)}"
	)
	assert in_any_ci - documented == set(), (
		f"run in CI but undocumented in CLAUDE.md: {sorted(in_any_ci - documented)}"
	)


def test_documented_migration_order_matches_production_runner():
	"""PR #50 review finding (Important): apply_vera_health_runs.py and
	apply_events_dispatch_dedup_index.py were both correctly added to
	CLAUDE.md and every CI workflow, but scripts/run_migrations.sh — the
	actual production deploy script — has its own separate, hand-maintained
	MIGRATIONS array and was never updated. A deployment using this script
	would never create vera_health_runs, so the health worker could never
	insert a row and every settlement/billing gate would read
	NO_HEALTH_RUN and halt indefinitely — a real, live-consequence miss
	that CI green could not catch, since this script isn't part of CI.

	This is the third place a migration must be registered (CLAUDE.md +
	both CI workflows already checked above), so it gets the same
	documented-vs-actual diff check, not a one-off manual fix."""
	runner = REPO_ROOT / "scripts" / "run_migrations.sh"
	if not runner.exists():
		pytest.skip("scripts/run_migrations.sh does not exist in this checkout")

	claude_md = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
	documented = set(re.findall(r"migrations/(apply_\w+)\.py", claude_md))

	runner_text = runner.read_text(encoding="utf-8")
	# The MIGRATIONS array holds bare script names, one per line, no .py
	# suffix and no migrations/ prefix (e.g. "  apply_vera_health_runs").
	array_match = re.search(r"MIGRATIONS=\((.*?)\)", runner_text, re.DOTALL)
	assert array_match is not None, (
		"scripts/run_migrations.sh no longer defines a MIGRATIONS=(...) array — "
		"this test's parsing assumption is stale, update it rather than deleting it."
	)
	in_runner = set(re.findall(r"(apply_\w+)", array_match.group(1)))

	assert documented - in_runner == set(), (
		"documented in CLAUDE.md but missing from scripts/run_migrations.sh's "
		f"MIGRATIONS array: {sorted(documented - in_runner)} — a real production "
		"deploy using this script would never create these tables."
	)
	assert in_runner - documented == set(), (
		f"present in scripts/run_migrations.sh but undocumented in CLAUDE.md: "
		f"{sorted(in_runner - documented)}"
	)
