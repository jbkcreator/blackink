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
	# Subtask 1.1.1 — Appointment operations. The blueprint's printed
	# `client_id UUID REFERENCES companies` is adapted to this repo's real
	# tenant boundary: a VARCHAR(40) client_id added directly to all four
	# appointment tables (see apply_appointment_ops.py), so each is scoped at
	# the row it is written on rather than through a parent join.
	"appointments": {"mode": "direct", "column": "client_id"},
	"confirmation_logs": {"mode": "direct", "column": "client_id"},
	"appointment_dispositions": {"mode": "direct", "column": "client_id"},
	"appointment_disputes": {"mode": "direct", "column": "client_id"},
	# Subtask 1.2.2 — Settlement engine. settlement_offer_config is
	# deliberately NOT registered here — it's global reference config (the
	# commercial-terms row, same class as payment_auth_offer_config /
	# entitlement_offers), not tenant-bearing.
	"pms_agreements": {"mode": "direct", "column": "client_id"},
	"settlement_transactions": {"mode": "direct", "column": "client_id"},
	# Reply Triage Agent inbound email intake.
	"inbound_messages": {"mode": "direct", "column": "client_id"},
	# Subtask 1.2.3 — Six Billing Rules. entitlement_offers is deliberately
	# NOT registered here — global reference config, same class as
	# settlement_offer_config / payment_auth_offer_config (see the "not
	# tenant-scoped" block below).
	"client_entitlements": {"mode": "direct", "column": "client_id"},
	"billing_credits": {"mode": "direct", "column": "client_id"},
	"subscription_overrides": {"mode": "direct", "column": "client_id"},
	# Subtask 3.1.1 — Lost-Owner CSV Ingest. Both carry their own client_id
	# column (winback_rows.client_id is denormalized from winback_imports at
	# insert time, same "direct mode needs its own column per table"
	# reasoning as meeting_outcome_prompt_jobs above).
	"winback_imports": {"mode": "direct", "column": "client_id"},
	"winback_rows": {"mode": "direct", "column": "client_id"},
	# Subtask 3.1.2 — Three-Touch Win-Back Sequence. At-most-once dispatch
	# claim table, mirrors sequence_touch_dispatches' own direct-mode entry
	# above, keyed on winback_row_id instead of run_id.
	"winback_touch_dispatches": {"mode": "direct", "column": "client_id"},
	# Audit-trail counterpart to compliance_gate_checks (above), for the
	# win-back touch gate — a separate table because compliance_gate_checks'
	# contact_id column is a hard FK to contacts, which a winback_row_id can
	# never satisfy correctly.
	"winback_gate_checks": {"mode": "direct", "column": "client_id"},
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
#   entitlement_offers, settlement_offer_config, payment_auth_offer_config —
#     global commercial-terms config rows, not tenant data. An operator flips
#     one per confirmed offer; none carries a client_id of its own.
