"""Single source of truth for which tables are tenant-scoped and how.

Both migrations/apply_rls_policies.py (which generates the actual Postgres
RLS policies from this) and tests/test_tenant_isolation.py (which uses this
to know what to probe) read from this registry, so the enforcement and the
test that verifies it can never silently drift apart.

Two scoping modes:
  "direct" — the table has its own client_id/owning_client_id column.
  "join"   — the table has no tenant column of its own (e.g. contacts,
             scoped through its parent company) and is protected via a
             correlated EXISTS subquery against the parent table's column.

Deliberate limitation, stated plainly rather than glossed over: this
registry is NOT auto-discovered from Base.metadata. A table with implicit
(join-based) tenant scoping can't be reliably detected by column
introspection alone, so it must be registered here explicitly. The residual
risk is a new tenant-bearing table shipping without an entry here — there is
no automatic net for that. Anyone adding a table that should be tenant-scoped
must add it to this dict, or it gets neither an RLS policy nor leakage-test
coverage.
"""

TENANT_POLICIES = {
	"companies": {"mode": "direct", "column": "owning_client_id"},
	"contacts": {
		"mode": "join",
		"join_table": "companies",
		"join_on": "company_id",
		"join_column": "owning_client_id",
	},
	"pm_profiles": {
		"mode": "join",
		"join_table": "companies",
		"join_on": "company_id",
		"join_column": "owning_client_id",
	},
	"clients": {"mode": "direct", "column": "client_id"},
	"county_allocations": {"mode": "direct", "column": "client_id"},
	"client_pm_books": {"mode": "direct", "column": "client_id"},
	"events": {"mode": "direct", "column": "client_id"},
	"compliance_gate_checks": {"mode": "direct", "column": "client_id"},
	"sending_domains": {"mode": "direct", "column": "client_id"},
	"mailboxes": {"mode": "direct", "column": "client_id"},
	"agent_work_orders": {"mode": "direct", "column": "client_id"},
}

# Tables deliberately NOT tenant-scoped, and why — kept here so the absence
# reads as a decision, not an oversight:
#   counties, owner_entities, owner_entity_links — global reference data.
#   raw_prospect_companies, raw_prospect_contacts — Akrash has no visibility
#     into the client roster by design; ownership is assigned only at
#     promotion time (see Dev 1 plan §Key decision 6).
