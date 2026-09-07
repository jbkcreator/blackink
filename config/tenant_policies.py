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
	"sms_dispatch_log": {"mode": "direct", "column": "client_id"},
	"sending_domains": {"mode": "direct", "column": "client_id"},
	"mailboxes": {"mode": "direct", "column": "client_id"},
	"agent_work_orders": {"mode": "direct", "column": "client_id"},
	"meeting_outcomes": {"mode": "direct", "column": "client_id"},
	"sequence_runs": {"mode": "direct", "column": "client_id"},
	"sequence_touch_dispatches": {"mode": "direct", "column": "client_id"},
	# owner_visibility_scores has no client_id column of its own — scoped through
	# companies.owning_client_id via company_id FK, same join pattern as contacts.
	"owner_visibility_scores": {
		"mode": "join",
		"join_table": "companies",
		"join_on": "company_id",
		"join_column": "owning_client_id",
	},
	"calendar_connections": {"mode": "direct", "column": "client_id"},
	"owner_contacts": {"mode": "direct", "column": "client_id"},
	"bookings": {"mode": "direct", "column": "client_id"},
	"oauth_connect_nonces": {"mode": "direct", "column": "client_id"},
	"calendar_sync_queue": {
		"mode": "join",
		"join_table": "calendar_connections",
		"join_on": "connection_id",
		"join_column": "client_id",
	},
	# booking_reminder_jobs has no client_id column of its own — scoped through
	# bookings.client_id via booking_id FK.
	"booking_reminder_jobs": {
		"mode": "join",
		"join_table": "bookings",
		"join_on": "booking_id",
		"join_column": "client_id",
	},
	# No-Show Handler. Both carry their own client_id column.
	"no_show_prompt_jobs": {"mode": "direct", "column": "client_id"},
	"no_show_recovery_jobs": {"mode": "direct", "column": "client_id"},
	# "Log Outcome" trigger card. Carries its own
	# client_id column (copied from bookings.client_id at schedule time).
	"meeting_outcome_prompt_jobs": {"mode": "direct", "column": "client_id"},
	# Reply Triage Agent inbound email intake.
	"inbound_messages": {"mode": "direct", "column": "client_id"},
}

# Tables deliberately NOT tenant-scoped, and why — kept here so the absence
# reads as a decision, not an oversight:
#   counties, owner_entities, owner_entity_links — global reference data.
#   raw_prospect_companies, raw_prospect_contacts — Akrash has no visibility
#     into the client roster by design; ownership is assigned only at
#     promotion time (ownership is assigned at promotion, not ingestion).
#   self_serve_audit_submissions — pre-company, pre-tenant
#     public landing-page staging data, same posture as raw_prospect_*;
#     ownership is assigned only once the worker resolves/creates a
#     companies row, never at submission time.
