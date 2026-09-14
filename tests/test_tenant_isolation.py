"""Adversarial cross-tenant leakage test suite.

Requires a real Postgres instance with migrations 1-11 applied (roles +
tables + RLS policies), pointed at by DATABASE_URL_APP / DATABASE_URL_SYSTEM.
Must pass before apply_rls_policies.py is considered done, and stays
required on every PR touching models.py / migrations/ / database.py.

Two layers, deliberately kept separate:
  1. Generic policy-existence check (test_rls_enabled_and_forced_on_every_
     registered_table) — introspects pg_tables/pg_policies against
     config/tenant_policies.py's TENANT_POLICIES registry, so a new table
     added there is automatically covered. This is what closes the gap
     that let Forced Action ship with zero enforcement.
  2. Targeted data-level probes (companies/contacts) using two synthetic
     canary tenants — proves the policy actually blocks a real cross-tenant
     read/write, not just that a policy object exists. Run with BOTH
     client_id set to the wrong canary AND with no client_id set at all,
     to verify the RLS backstop holds even when app-layer context is
     missing entirely (the scenario a code bug, not just a malicious
     query, would produce).
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from config.tenant_policies import TENANT_POLICIES
from src.core.database import Database, get_db_context, get_owner_db_context, get_system_db_context
from src.services.campaign_readiness_gate import evaluate_full_readiness
from src.services.compliance_gate import DncProvider
from src.services.sms_dispatch import (
	ColdSMSBlockedError,
	SmsDispatchNeedsReconciliationError,
	SmsProvider,
	dispatch_sms,
)
from tests.fixtures.synthetic_tenants import CANARY_A, CANARY_B, canary_tenants  # noqa: F401


class _CountingSmsProvider(SmsProvider):
	def __init__(self):
		self.calls = []

	def send(self, phone, message, idempotency_key):
		self.calls.append((phone, message, idempotency_key))
		return "stub-message-id"


class _FixedDnc(DncProvider):
	"""Test double — always returns a fixed listed/clear verdict, and counts
	calls so cache-hit-avoids-live-call can be asserted."""

	def __init__(self, listed: bool):
		self.listed = listed
		self.calls = 0

	def check(self, phone):
		self.calls += 1
		return self.listed


def test_rls_enabled_and_forced_on_every_registered_table():
	db = Database()
	with db.session_scope() as session:
		# pg_tables has no forcerowsecurity column — see
		# migrations/apply_rls_policies.py's verification query for why this
		# reads from pg_class/pg_namespace instead.
		rows = session.execute(
			text(
				"SELECT c.relname AS tablename, c.relrowsecurity AS rowsecurity, "
				"c.relforcerowsecurity AS forcerowsecurity "
				"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
				"WHERE n.nspname = 'public' AND c.relname = ANY(:tables)"
			),
			{"tables": list(TENANT_POLICIES.keys())},
		).fetchall()
		by_table = {r.tablename: r for r in rows}

		for table in TENANT_POLICIES:
			row = by_table.get(table)
			assert row is not None, f"{table}: table not found — has apply_rls_policies.py run?"
			assert row.rowsecurity, f"{table}: rowsecurity not enabled"
			assert row.forcerowsecurity, f"{table}: forcerowsecurity not enabled"

		policy_rows = session.execute(
			text(
				"SELECT tablename FROM pg_policies WHERE schemaname = 'public' "
				"AND policyname = 'tenant_isolation' AND tablename = ANY(:tables)"
			),
			{"tables": list(TENANT_POLICIES.keys())},
		).fetchall()
		tables_with_policy = {r.tablename for r in policy_rows}
		for table in TENANT_POLICIES:
			assert table in tables_with_policy, f"{table}: tenant_isolation policy missing"


def test_cross_tenant_company_read_blocked(canary_tenants):
	other_company_id = canary_tenants[CANARY_B]["company_id"]

	with get_db_context(client_id=CANARY_A) as session:
		rows = session.execute(
			text("SELECT 1 FROM companies WHERE company_id = :cid"), {"cid": other_company_id}
		).fetchall()
	assert rows == [], "Canary A read Canary B's company row under RLS"


def test_cross_tenant_company_write_blocked(canary_tenants):
	other_company_id = canary_tenants[CANARY_B]["company_id"]

	with get_db_context(client_id=CANARY_A) as session:
		result = session.execute(
			text("UPDATE companies SET door_count_est = 999 WHERE company_id = :cid"),
			{"cid": other_company_id},
		)
	assert result.rowcount == 0, "Canary A updated Canary B's company row under RLS"


def test_cross_tenant_contact_read_blocked_via_join(canary_tenants):
	"""contacts has no direct client_id column — scoped via the join policy
	against companies.owning_client_id. Proves the join-mode policy works,
	not just the direct-column mode."""
	other_contact_id = canary_tenants[CANARY_B]["contact_id"]

	with get_db_context(client_id=CANARY_A) as session:
		rows = session.execute(
			text("SELECT 1 FROM contacts WHERE contact_id = :cid"), {"cid": other_contact_id}
		).fetchall()
	assert rows == [], "Canary A read Canary B's contact row under RLS (join-mode policy)"


def test_rls_backstop_holds_with_no_client_context_at_all(canary_tenants):
	"""The scenario a code bug (not just a malicious query) would produce:
	a session with no client_id set at all must still see nothing on a
	tenant table — RLS is the backstop even when app-layer context is
	entirely absent, not just wrong."""
	other_company_id = canary_tenants[CANARY_A]["company_id"]

	with get_db_context() as session:  # no client_id
		rows = session.execute(
			text("SELECT 1 FROM companies WHERE company_id = :cid"), {"cid": other_company_id}
		).fetchall()
	assert rows == [], "A session with no tenant context read a tenant-scoped row"


def test_only_blackink_app_can_execute_non_poach_function():
	"""akrash_ingest has INSERT-only grants on the two staging tables and
	nothing else. Postgres grants EXECUTE on every new function to PUBLIC by
	default; a prior version of apply_compliance_gate_audit.py never revoked
	it, so this SECURITY DEFINER function (which bypasses RLS) was callable
	by a role with zero table-read grants — a restricted ingestion credential
	could still probe cross-tenant non-poach claims through it. Checked via
	Postgres's own has_function_privilege() rather than a live connection as
	akrash_ingest, since pg_hba/firewall network restrictions may legitimately
	block that role from ever reaching the DB from outside its ingest path —
	this asserts the grant itself, independent of network topology."""
	db = Database()
	with db.session_scope() as session:
		for role in ("akrash_ingest", "blackink_system"):
			allowed = session.execute(
				text(
					"SELECT has_function_privilege(:role, "
					"'is_claimed_by_other_client(varchar)', 'EXECUTE')"
				),
				{"role": role},
			).scalar()
			assert allowed is False, f"{role} can execute is_claimed_by_other_client — should be denied"

		# has_function_privilege() takes a real role name, so PUBLIC (an
		# implicit grantee, not a row in pg_roles) can't be checked that way —
		# inspect the ACL array directly for the unnamed-grantee entry
		# ('=...', no role name before '=') that a PUBLIC grant produces.
		has_public_grant = session.execute(
			text(
				"SELECT EXISTS ("
				"  SELECT 1 FROM pg_proc p, unnest(p.proacl) AS acl"
				"  WHERE p.proname = 'is_claimed_by_other_client' AND acl::text LIKE '=%'"
				")"
			)
		).scalar()
		assert has_public_grant is False, "PUBLIC still holds an EXECUTE grant on is_claimed_by_other_client"

		allowed = session.execute(
			text(
				"SELECT has_function_privilege('blackink_app', "
				"'is_claimed_by_other_client(varchar)', 'EXECUTE')"
			)
		).scalar()
		assert allowed is True, "blackink_app should retain EXECUTE — the app is the only caller"


def test_akrash_ingest_has_no_access_to_assessor_parcels():
	"""S-24 rebuild: raw_assessor_parcels (renamed assessor_parcels by
	apply_assessor_sync.py) was never actually Akrash's to write — that
	assumption traced to nothing in the client's build spec, and the
	table sat empty because of it. apply_akrash_grant.py no longer grants
	akrash_ingest anything on this table at all; the real source is now
	src/tasks/assessor_sync.py's own daily scrape, run under
	blackink_system. blackink_system holds SELECT/INSERT/UPDATE but not
	DELETE (a parcel is soft-retired via retired_at, never row-deleted at
	runtime — see apply_assessor_sync.py's grant list). Checked via
	has_table_privilege() rather than a live connection as akrash_ingest,
	same reasoning as test_only_blackink_app_can_execute_non_poach_function
	above (pg_hba/firewall restrictions may legitimately block that role
	from reaching the DB from outside its ingest path)."""
	db = Database()
	with db.session_scope() as session:
		for privilege in ("INSERT", "SELECT", "UPDATE", "DELETE"):
			has_it = session.execute(
				text("SELECT has_table_privilege('akrash_ingest', 'assessor_parcels', :priv)"),
				{"priv": privilege},
			).scalar()
			assert has_it is False, f"akrash_ingest must NOT be able to {privilege} assessor_parcels"

		# blackink_system is the daily sync's own write path (BYPASSRLS).
		system_can_select = session.execute(
			text("SELECT has_table_privilege('blackink_system', 'assessor_parcels', 'SELECT')")
		).scalar()
		assert system_can_select is True, "blackink_system should have SELECT on assessor_parcels"

		system_can_insert = session.execute(
			text("SELECT has_table_privilege('blackink_system', 'assessor_parcels', 'INSERT')")
		).scalar()
		assert system_can_insert is True, "blackink_system should have INSERT on assessor_parcels"

		system_can_update = session.execute(
			text("SELECT has_table_privilege('blackink_system', 'assessor_parcels', 'UPDATE')")
		).scalar()
		assert system_can_update is True, "blackink_system should have UPDATE on assessor_parcels"

		system_can_delete = session.execute(
			text("SELECT has_table_privilege('blackink_system', 'assessor_parcels', 'DELETE')")
		).scalar()
		assert system_can_delete is False, "blackink_system must NOT be able to DELETE assessor_parcels — parcels are soft-retired"


def test_non_poach_function_discloses_no_identity(canary_tenants):
	"""is_claimed_by_other_client returns a boolean only — the requesting
	client must never learn WHICH other client owns a claimed company."""
	# Seed canary A's company into canary B's own PM book (client B legitimately
	# writing a row it owns) so it reads as claimed. Uses get_db_context(client_id=
	# CANARY_B), not a bare/unscoped session — an unscoped write is correctly
	# rejected by RLS's WITH CHECK (client_id must equal the session's tenant
	# context), which is RLS working as intended, not something to route around.
	with get_db_context(client_id=CANARY_B) as session:
		session.execute(
			text(
				"INSERT INTO client_pm_books (client_id, owner_domain) VALUES (:cid, :domain)"
			),
			{"cid": CANARY_B, "domain": canary_tenants[CANARY_A]["domain"]},
		)

	try:
		with get_db_context(client_id=CANARY_A) as session:
			claimed = session.execute(
				text("SELECT is_claimed_by_other_client(:company_id) AS claimed"),
				{"company_id": canary_tenants[CANARY_A]["company_id"]},
			).scalar()
		assert claimed is True
	finally:
		# Always clean up the seeded row, even if the assert above fails —
		# otherwise the canary_tenants fixture's own teardown hits a foreign
		# key violation trying to delete a client still referenced here.
		with get_db_context(client_id=CANARY_B) as session:
			session.execute(
				text("DELETE FROM client_pm_books WHERE client_id = :cid"), {"cid": CANARY_B}
			)


# ── Subtask 1.2.1 — evaluate_campaign_readiness() Check 1 + Check 2 ──


def test_campaign_readiness_permanently_blocks_opted_out_contact(canary_tenants):
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET is_opted_out = TRUE WHERE contact_id = :cid"), {"cid": contact_id}
		)

	with get_db_context(client_id=CANARY_A) as session:
		status = session.execute(
			text("SELECT evaluate_campaign_readiness(:cid)"), {"cid": contact_id}
		).scalar()
	assert status == "PERMANENTLY_BLOCKED"


def test_campaign_readiness_permanently_blocks_suppressed_contact(canary_tenants):
	contact_id = canary_tenants[CANARY_B]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET suppression_state = TRUE WHERE contact_id = :cid"), {"cid": contact_id}
		)

	with get_db_context(client_id=CANARY_B) as session:
		status = session.execute(
			text("SELECT evaluate_campaign_readiness(:cid)"), {"cid": contact_id}
		).scalar()
	assert status == "PERMANENTLY_BLOCKED"


def test_campaign_readiness_suppresses_non_poach_domain_match_and_logs_event(canary_tenants):
	"""Canary A's own company domain is seeded into Canary B's PM book —
	evaluating Canary A's contact under Canary A's own tenant context must
	see the conflict (Check 2 excludes only the REQUESTING client's own
	book rows, not the target's), return SUPPRESSED, and log a
	non_poach_suppressed event naming the matching client_id."""
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	domain = canary_tenants[CANARY_A]["domain"]

	with get_db_context(client_id=CANARY_B) as session:
		session.execute(
			text("INSERT INTO client_pm_books (client_id, owner_domain) VALUES (:cid, :domain)"),
			{"cid": CANARY_B, "domain": domain},
		)
	try:
		with get_db_context(client_id=CANARY_A) as session:
			status = session.execute(
				text("SELECT evaluate_campaign_readiness(:cid)"), {"cid": contact_id}
			).scalar()
			event = session.execute(
				text(
					"SELECT payload FROM events WHERE event_type = 'non_poach_suppressed' "
					"AND entity_id = :cid ORDER BY created_at DESC LIMIT 1"
				),
				{"cid": str(contact_id)},
			).first()
		assert status == "SUPPRESSED"
		assert event is not None, "non_poach_suppressed event not logged"
		assert event.payload["matched_client_id"] == CANARY_B
	finally:
		with get_db_context(client_id=CANARY_B) as session:
			session.execute(text("DELETE FROM client_pm_books WHERE client_id = :cid"), {"cid": CANARY_B})
		# The function's own INSERT into events (owned by the requesting
		# client, CANARY_A here) must be cleaned up too — canary_tenants'
		# teardown deletes the clients row afterward, and events.client_id
		# has a NOT NULL FK to clients, so a leftover event row would break
		# every test that runs after this one. events is deliberately
		# append-only (apply_events.py grants blackink_app/blackink_system
		# SELECT+INSERT only, no DELETE) — the owner/superuser context
		# (what migrations run as) is the only role that can clean it up.
		with get_owner_db_context() as session:
			session.execute(
				text(
					"DELETE FROM events WHERE event_type = 'non_poach_suppressed' AND entity_id = :cid"
				),
				{"cid": str(contact_id)},
			)
			session.commit()


def test_campaign_readiness_suppresses_non_poach_email_match_and_logs_event(canary_tenants):
	contact_id = canary_tenants[CANARY_B]["contact_id"]
	domain = canary_tenants[CANARY_B]["domain"]
	email = f"owner@{domain}"

	with get_db_context(client_id=CANARY_A) as session:
		session.execute(
			text("INSERT INTO client_pm_books (client_id, owner_email) VALUES (:cid, :email)"),
			{"cid": CANARY_A, "email": email},
		)
	try:
		with get_db_context(client_id=CANARY_B) as session:
			status = session.execute(
				text("SELECT evaluate_campaign_readiness(:cid)"), {"cid": contact_id}
			).scalar()
			event = session.execute(
				text(
					"SELECT payload FROM events WHERE event_type = 'non_poach_suppressed' "
					"AND entity_id = :cid ORDER BY created_at DESC LIMIT 1"
				),
				{"cid": str(contact_id)},
			).first()
		assert status == "SUPPRESSED"
		assert event is not None, "non_poach_suppressed event not logged"
		assert event.payload["matched_client_id"] == CANARY_A
	finally:
		with get_db_context(client_id=CANARY_A) as session:
			session.execute(text("DELETE FROM client_pm_books WHERE client_id = :cid"), {"cid": CANARY_A})
		# See the domain-match test above for why this cleanup is required
		# and why it must go through the owner context.
		with get_owner_db_context() as session:
			session.execute(
				text(
					"DELETE FROM events WHERE event_type = 'non_poach_suppressed' AND entity_id = :cid"
				),
				{"cid": str(contact_id)},
			)
			session.commit()


def test_campaign_readiness_passes_clean_contact(canary_tenants):
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_db_context(client_id=CANARY_A) as session:
		status = session.execute(
			text("SELECT evaluate_campaign_readiness(:cid)"), {"cid": contact_id}
		).scalar()
	assert status == "PASS"


def test_only_blackink_app_can_execute_campaign_readiness_function():
	db = Database()
	with db.session_scope() as session:
		for role in ("akrash_ingest", "blackink_system"):
			allowed = session.execute(
				text(
					"SELECT has_function_privilege(:role, "
					"'evaluate_campaign_readiness(bigint)', 'EXECUTE')"
				),
				{"role": role},
			).scalar()
			assert allowed is False, f"{role} can execute evaluate_campaign_readiness — should be denied"

		has_public_grant = session.execute(
			text(
				"SELECT EXISTS ("
				"  SELECT 1 FROM pg_proc p, unnest(p.proacl) AS acl"
				"  WHERE p.proname = 'evaluate_campaign_readiness' AND acl::text LIKE '=%'"
				")"
			)
		).scalar()
		assert has_public_grant is False, "PUBLIC still holds an EXECUTE grant on evaluate_campaign_readiness"

		allowed = session.execute(
			text(
				"SELECT has_function_privilege('blackink_app', "
				"'evaluate_campaign_readiness(bigint)', 'EXECUTE')"
			)
		).scalar()
		assert allowed is True, "blackink_app should retain EXECUTE — the app is the only caller"


def test_campaign_readiness_requires_tenant_context(canary_tenants):
	"""A clean contact reaches Check 2, which needs a real client_id to
	attribute the audit event to — missing tenant context must raise, not
	silently guess or return a status."""
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_db_context() as session:  # no client_id
		try:
			session.execute(text("SELECT evaluate_campaign_readiness(:cid)"), {"cid": contact_id})
			raised = False
		except Exception:
			raised = True
	assert raised, "evaluate_campaign_readiness did not raise with no tenant context set"


def test_campaign_readiness_rejects_cross_tenant_contact_id(canary_tenants):
	"""evaluate_campaign_readiness is SECURITY DEFINER and bypasses RLS, so
	the contact_id -> companies.owning_client_id ownership check inside the
	function is the only thing standing between a caller and another
	tenant's contact. Canary B must not be able to read Canary A's contact
	by ID, whether or not it happens to be opted out — either way it must
	raise, never return a status."""
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET is_opted_out = TRUE WHERE contact_id = :cid"), {"cid": contact_id}
		)

	with get_db_context(client_id=CANARY_B) as session:
		try:
			session.execute(text("SELECT evaluate_campaign_readiness(:cid)"), {"cid": contact_id})
			raised = False
		except Exception:
			raised = True
	assert raised, "evaluate_campaign_readiness leaked another tenant's contact by ID"


# ── Subtask 1.2.2 — Checks 3+4 (DNC / quiet hours / warm-channel waterfall) ──
#
# Per the master blueprint (§3.1.2's waterfall diagram + §3.0.4's CI-enforced
# predicate), NOT the Week1_Tasks_Dev_Split.md DoD checklist's more ambiguous
# wording: a DNC hit is a partial channel restriction ("CHANNEL SUPPRESSED"),
# not a full block like Checks 1/2 — a DNC-listed contact still gets
# readiness=TRUE and EMAIL_COLD_ELIGIBLE, never BLOCKED.


def _cleanup_compliance_events(contact_id):
	# compliance_gate_evaluated (and, for the "engaged" tests below,
	# meeting_booked) rows are append-only (blackink_app/system have no
	# DELETE grant on events) — same cleanup pattern as the
	# non_poach_suppressed tests above, via the owner context, before the
	# canary_tenants fixture's own teardown deletes the referencing client.
	with get_owner_db_context() as session:
		session.execute(
			text(
				"DELETE FROM events WHERE event_type IN ('compliance_gate_evaluated', 'meeting_booked') "
				"AND entity_id = :cid"
			),
			{"cid": str(contact_id)},
		)
		session.commit()


def _seed_meeting_booked_event(contact_id, client_id):
	# D-7 fix (audit 2026-09-10): is_engaged()'s booked-appointment signal
	# now comes from cold_sms_gate.get_booked_appointment_id(), which reads
	# the events ledger (event_type='meeting_booked'), not the
	# contacts.booked_appointment_id column — that denormalized column is
	# never written by any production path, so setting it directly (the
	# pre-fix version of these tests) would silently prove nothing anymore.
	with get_system_db_context() as session:
		session.execute(
			text(
				"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
				"VALUES (:client_id, 'meeting_booked', 'contact', :cid, '{}'::jsonb)"
			),
			{"client_id": client_id, "cid": str(contact_id)},
		)
		session.commit()


def test_full_readiness_florida_dnc_listed_engaged_contact_withholds_sms_not_blocked(canary_tenants):
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"),
			{"cid": contact_id},
		)
	_seed_meeting_booked_event(contact_id, CANARY_A)
	try:
		with get_db_context(client_id=CANARY_A) as session:
			result = evaluate_full_readiness(session, contact_id, CANARY_A, dnc_provider=_FixedDnc(listed=True))
		assert result.readiness is True
		assert result.compliance_eligibility == "EMAIL_COLD_ELIGIBLE"
		assert result.reason_code == "ENGAGED_DNC_SMS_WITHHELD"
	finally:
		_cleanup_compliance_events(contact_id)


def test_full_readiness_clean_unengaged_contact_gets_email_cold_eligible(canary_tenants):
	contact_id = canary_tenants[CANARY_B]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"), {"cid": contact_id}
		)
	try:
		with get_db_context(client_id=CANARY_B) as session:
			result = evaluate_full_readiness(session, contact_id, CANARY_B, dnc_provider=_FixedDnc(listed=False))
		assert result.readiness is True
		assert result.compliance_eligibility == "EMAIL_COLD_ELIGIBLE"
		assert result.reason_code == "COLD_EMAIL_ONLY"
	finally:
		_cleanup_compliance_events(contact_id)


def test_full_readiness_booked_contact_daytime_gets_transactional_sms(canary_tenants, monkeypatch):
	import src.services.campaign_readiness_gate as gate_module

	monkeypatch.setattr(gate_module, "_local_hour", lambda tz_name: 14)  # 2pm — outside quiet hours
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"),
			{"cid": contact_id},
		)
	_seed_meeting_booked_event(contact_id, CANARY_A)
	try:
		with get_db_context(client_id=CANARY_A) as session:
			result = evaluate_full_readiness(session, contact_id, CANARY_A, dnc_provider=_FixedDnc(listed=False))
		assert result.readiness is True
		assert result.compliance_eligibility == "TRANSACTIONAL_SMS_ONLY"
		assert result.reason_code == "ENGAGED_SMS_ELIGIBLE"
	finally:
		_cleanup_compliance_events(contact_id)


def test_full_readiness_booked_contact_quiet_hours_withholds_sms(canary_tenants, monkeypatch):
	import src.services.campaign_readiness_gate as gate_module

	monkeypatch.setattr(gate_module, "_local_hour", lambda tz_name: 22)  # 10pm — quiet hours
	contact_id = canary_tenants[CANARY_B]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"),
			{"cid": contact_id},
		)
	_seed_meeting_booked_event(contact_id, CANARY_B)
	try:
		with get_db_context(client_id=CANARY_B) as session:
			result = evaluate_full_readiness(session, contact_id, CANARY_B, dnc_provider=_FixedDnc(listed=False))
		assert result.readiness is True
		assert result.compliance_eligibility == "EMAIL_COLD_ELIGIBLE"
		assert result.reason_code == "ENGAGED_QUIET_HOURS_SMS_WITHHELD"
	finally:
		_cleanup_compliance_events(contact_id)


def test_full_readiness_dnc_cache_populated_then_reused(canary_tenants):
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"), {"cid": contact_id}
		)
	provider = _FixedDnc(listed=False)
	try:
		with get_db_context(client_id=CANARY_A) as session:
			evaluate_full_readiness(session, contact_id, CANARY_A, dnc_provider=provider)
		assert provider.calls == 1, "first call (cache empty) should query the live provider"

		with get_db_context(client_id=CANARY_A) as session:
			evaluate_full_readiness(session, contact_id, CANARY_A, dnc_provider=provider)
		assert provider.calls == 1, "second call (cache fresh) should reuse the cached value"
	finally:
		_cleanup_compliance_events(contact_id)


def test_full_readiness_logs_compliance_gate_evaluated_event_with_reason_code(canary_tenants):
	contact_id = canary_tenants[CANARY_B]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"), {"cid": contact_id}
		)
	try:
		with get_db_context(client_id=CANARY_B) as session:
			evaluate_full_readiness(session, contact_id, CANARY_B, dnc_provider=_FixedDnc(listed=False))
			event = session.execute(
				text(
					"SELECT payload FROM events WHERE event_type = 'compliance_gate_evaluated' "
					"AND entity_id = :cid ORDER BY created_at DESC LIMIT 1"
				),
				{"cid": str(contact_id)},
			).first()
		assert event is not None, "compliance_gate_evaluated event not logged"
		assert event.payload["reason_code"] == "COLD_EMAIL_ONLY"
		assert event.payload["compliance_eligibility"] == "EMAIL_COLD_ELIGIBLE"
	finally:
		_cleanup_compliance_events(contact_id)


def test_full_readiness_end_to_end_cold_clean_contact(canary_tenants):
	"""Integration per the DoD: a contact clearing all four checks returns
	readiness=TRUE and EMAIL_COLD_ELIGIBLE (cold — no prior engagement)."""
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"), {"cid": contact_id}
		)
	try:
		with get_db_context(client_id=CANARY_A) as session:
			result = evaluate_full_readiness(session, contact_id, CANARY_A, dnc_provider=_FixedDnc(listed=False))
		assert result.readiness is True
		assert result.compliance_eligibility == "EMAIL_COLD_ELIGIBLE"

		with get_db_context(client_id=CANARY_A) as session:
			stored = session.execute(
				text("SELECT compliance_eligibility FROM contacts WHERE contact_id = :cid"), {"cid": contact_id}
			).scalar()
		assert stored == "EMAIL_COLD_ELIGIBLE", "final eligibility must be written back to contacts"
	finally:
		_cleanup_compliance_events(contact_id)


# ── Subtask 1.2.3 — Cold SMS Hard Block (DB / application / CI/CD layers) ──


def _cleanup_cold_sms_events(contact_id):
	# dispatch_sms() now always runs a fresh evaluate_full_readiness() first
	# (PR #10 review fixup), which logs its own compliance_gate_evaluated
	# event on every call — clean that up too, same reason
	# _cleanup_compliance_events exists: events.client_id has a NOT NULL FK
	# to clients, so a leftover row breaks canary_tenants' teardown.
	with get_owner_db_context() as session:
		session.execute(
			text(
				"DELETE FROM events WHERE event_type IN "
				"('cold_sms_blocked', 'compliance_gate_evaluated', 'sms_inbound') "
				"AND entity_id = :cid"
			),
			{"cid": str(contact_id)},
		)
		session.execute(text("DELETE FROM sms_dispatch_log WHERE contact_id = :cid"), {"cid": contact_id})
		session.commit()


def _seed_inbound_sms_event(contact_id, client_id):
	# D-7 fix (audit 2026-09-10): is_engaged()'s inbound-SMS signal now comes
	# from cold_sms_gate.get_inbound_sms_count(), which reads the events
	# ledger (event_type in reply_received/sms_inbound), not the
	# contacts.inbound_sms_count column — that denormalized column is never
	# written by any production path, so setting it directly (the pre-fix
	# version of these tests) would silently prove nothing anymore.
	with get_system_db_context() as session:
		session.execute(
			text(
				"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
				"VALUES (:client_id, 'sms_inbound', 'contact', :cid, '{}'::jsonb)"
			),
			{"client_id": client_id, "cid": str(contact_id)},
		)
		session.commit()


def test_sms_dispatch_log_db_constraint_rejects_cold_insert(canary_tenants):
	"""DB layer: a raw SQL INSERT for a cold contact (no engagement) is
	rejected by ck_sms_dispatch_log_not_cold regardless of application code.
	The test itself logs the cold_sms_blocked/DATABASE event on catching the
	violation, since a rolled-back transaction can't log anything from
	inside itself."""
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	raised = False
	try:
		# Letting the IntegrityError propagate out of the `with` block (rather
		# than catching it inline) so get_db_context()'s own except-rollback
		# handles the aborted transaction — committing a session after
		# swallowing a DB error inside the block would itself raise.
		with get_db_context(client_id=CANARY_A) as session:
			session.execute(
				text(
					"INSERT INTO sms_dispatch_log "
					"(client_id, contact_id, inbound_sms_count_at_send, booked_appointment_id_at_send) "
					"VALUES (:client_id, :contact_id, 0, NULL)"
				),
				{"client_id": CANARY_A, "contact_id": contact_id},
			)
	except IntegrityError:
		raised = True
	assert raised, "cold sms_dispatch_log INSERT should violate ck_sms_dispatch_log_not_cold"

	try:
		with get_db_context(client_id=CANARY_A) as session:
			session.execute(
				text(
					"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
					"VALUES (:client_id, 'cold_sms_blocked', 'contact', :entity_id, "
					"jsonb_build_object('layer', 'DATABASE'))"
				),
				{"client_id": CANARY_A, "entity_id": str(contact_id)},
			)
	finally:
		_cleanup_cold_sms_events(contact_id)


def test_sms_dispatch_log_db_constraint_allows_engaged_insert(canary_tenants):
	contact_id = canary_tenants[CANARY_B]["contact_id"]
	try:
		with get_db_context(client_id=CANARY_B) as session:
			session.execute(
				text(
					"INSERT INTO sms_dispatch_log "
					"(client_id, contact_id, inbound_sms_count_at_send, booked_appointment_id_at_send) "
					"VALUES (:client_id, :contact_id, 0, 'appt_db_test')"
				),
				{"client_id": CANARY_B, "contact_id": contact_id},
			)
	finally:
		_cleanup_cold_sms_events(contact_id)


def test_dispatch_sms_application_layer_blocks_cold_contact_before_provider_call(canary_tenants):
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	provider = _CountingSmsProvider()
	try:
		with get_db_context(client_id=CANARY_A) as session:
			try:
				dispatch_sms(session, contact_id, CANARY_A, "hi", sms_provider=provider)
				raised = False
			except ColdSMSBlockedError:
				raised = True
			event = session.execute(
				text(
					"SELECT payload FROM events WHERE event_type = 'cold_sms_blocked' "
					"AND entity_id = :cid ORDER BY created_at DESC LIMIT 1"
				),
				{"cid": str(contact_id)},
			).first()
		assert raised, "dispatch_sms did not raise ColdSMSBlockedError for a cold contact"
		assert provider.calls == [], "provider.send must never be called for a cold contact"
		assert event is not None, "cold_sms_blocked event not logged"
		assert event.payload["layer"] == "APPLICATION"
	finally:
		_cleanup_cold_sms_events(contact_id)


def test_dispatch_sms_engaged_contact_passes_all_layers_and_reaches_provider(canary_tenants, monkeypatch):
	"""DoD: a legitimately consented contact successfully passes all three
	layers and reaches the (stubbed) Twilio call."""
	import src.services.campaign_readiness_gate as gate_module

	# Otherwise this test's pass/fail depends on the real wall-clock hour in
	# America/New_York (813 area code) when CI happens to run it — it must
	# not go quiet-hours-withheld just because CI ran at 2am Eastern.
	monkeypatch.setattr(gate_module, "_local_hour", lambda tz_name: 14)  # 2pm — outside quiet hours
	contact_id = canary_tenants[CANARY_B]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"),
			{"cid": contact_id},
		)
	_seed_inbound_sms_event(contact_id, CANARY_B)
	provider = _CountingSmsProvider()
	try:
		with get_db_context(client_id=CANARY_B) as session:
			# StubDncProvider's default None ("unknown") now correctly withholds
			# SMS since the PR #9 tri-state fix — an explicit clear result is
			# needed here to reach TRANSACTIONAL_SMS_ONLY at all.
			message_id = dispatch_sms(
				session,
				contact_id,
				CANARY_B,
				"your appointment is confirmed",
				sms_provider=provider,
				dnc_provider=_FixedDnc(listed=False),
			)

			log_row = session.execute(
				text(
					"SELECT status, provider_message_id FROM sms_dispatch_log "
					"WHERE contact_id = :cid ORDER BY created_at DESC LIMIT 1"
				),
				{"cid": contact_id},
			).first()
		assert message_id == "stub-message-id"
		assert len(provider.calls) == 1
		assert provider.calls[0][:2] == ("+18135550100", "your appointment is confirmed")
		assert log_row is not None, "sms_dispatch_log row not written for a successful send"
		assert log_row.status == "SENT"
		assert log_row.provider_message_id == "stub-message-id"
	finally:
		_cleanup_cold_sms_events(contact_id)


def test_dispatch_sms_writes_a_unique_idempotency_key_before_sending(canary_tenants, monkeypatch):
	"""PR #10 review fixup: the PENDING outbox row (and its idempotency_key)
	must exist and be committed independently of the SENT row's own
	transaction — proven end to end here against a real Postgres, not just
	the mocked open_outbox_session unit tests in test_sms_dispatch.py."""
	import src.services.campaign_readiness_gate as gate_module

	# See test_dispatch_sms_engaged_contact_passes_all_layers_and_reaches_provider
	# above for why this must not depend on the real wall-clock hour.
	monkeypatch.setattr(gate_module, "_local_hour", lambda tz_name: 14)  # 2pm — outside quiet hours
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"),
			{"cid": contact_id},
		)
	_seed_inbound_sms_event(contact_id, CANARY_A)
	provider = _CountingSmsProvider()
	try:
		with get_db_context(client_id=CANARY_A) as session:
			dispatch_sms(
				session,
				contact_id,
				CANARY_A,
				"reminder",
				sms_provider=provider,
				dnc_provider=_FixedDnc(listed=False),
			)
			log_row = session.execute(
				text(
					"SELECT status, idempotency_key FROM sms_dispatch_log "
					"WHERE contact_id = :cid ORDER BY created_at DESC LIMIT 1"
				),
				{"cid": contact_id},
			).first()
		assert log_row.status == "SENT"
		assert log_row.idempotency_key, "idempotency_key was not written"
	finally:
		_cleanup_cold_sms_events(contact_id)


def test_dispatch_sms_blocks_opted_out_engaged_contact_end_to_end(canary_tenants):
	"""Finding 1 regression: an opted-out contact that also happens to look
	'engaged' (inbound_sms_count > 0) must still be blocked — before this
	fix, dispatch_sms only checked is_engaged() and would have sent to
	them. Uses the real evaluate_full_readiness() path, not a mock, to
	prove the wiring actually works end to end."""
	contact_id = canary_tenants[CANARY_B]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text(
				"UPDATE contacts SET phone = '+18135550100', is_opted_out = TRUE "
				"WHERE contact_id = :cid"
			),
			{"cid": contact_id},
		)
	_seed_inbound_sms_event(contact_id, CANARY_B)
	provider = _CountingSmsProvider()
	try:
		with get_db_context(client_id=CANARY_B) as session:
			with pytest.raises(ColdSMSBlockedError):
				dispatch_sms(session, contact_id, CANARY_B, "hi", sms_provider=provider)
		assert provider.calls == [], "an opted-out contact must never reach the provider, engaged or not"
	finally:
		_cleanup_cold_sms_events(contact_id)


def test_dispatch_sms_blocked_audit_event_survives_exception_propagating_out_of_session_scope(canary_tenants):
	"""Finding 3 regression: unlike
	test_dispatch_sms_application_layer_blocks_cold_contact_before_provider_call
	above (which catches ColdSMSBlockedError *inside* the get_db_context()
	block — the exact pattern the review flagged as masking the bug), this
	lets the exception propagate all the way out of the `with` block and
	trigger session_scope()'s own except-rollback, then checks the audit
	event in a completely separate session/transaction. Only passes because
	the blocked-audit write now goes through its own independently
	committed outbox transaction, not the caller's (now-rolled-back) one."""
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	provider = _CountingSmsProvider()

	try:
		with pytest.raises(ColdSMSBlockedError):
			with get_db_context(client_id=CANARY_A) as session:
				dispatch_sms(session, contact_id, CANARY_A, "hi", sms_provider=provider)

		assert provider.calls == []

		with get_db_context(client_id=CANARY_A) as session:
			event = session.execute(
				text(
					"SELECT payload FROM events WHERE event_type = 'cold_sms_blocked' "
					"AND entity_id = :cid ORDER BY created_at DESC LIMIT 1"
				),
				{"cid": str(contact_id)},
			).first()
		assert event is not None, (
			"cold_sms_blocked audit event did not survive the caller's session_scope() rollback"
		)
		assert event.payload["layer"] == "APPLICATION"
	finally:
		_cleanup_cold_sms_events(contact_id)


def test_dispatch_sms_retry_with_sent_idempotency_key_does_not_call_provider_again(canary_tenants, monkeypatch):
	"""2nd PR #10 review fixup, end to end: retrying dispatch_sms() with the
	SAME idempotency_key after a successful send must return the prior
	provider_message_id and never touch the provider a second time —
	proven against the real (client_id, idempotency_key) UNIQUE constraint,
	not a mock."""
	import src.services.campaign_readiness_gate as gate_module

	monkeypatch.setattr(gate_module, "_local_hour", lambda tz_name: 14)
	contact_id = canary_tenants[CANARY_B]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"),
			{"cid": contact_id},
		)
	_seed_inbound_sms_event(contact_id, CANARY_B)
	provider = _CountingSmsProvider()
	try:
		with get_db_context(client_id=CANARY_B) as session:
			first_message_id = dispatch_sms(
				session,
				contact_id,
				CANARY_B,
				"reminder",
				sms_provider=provider,
				dnc_provider=_FixedDnc(listed=False),
				idempotency_key="retry-key-1",
			)
			second_message_id = dispatch_sms(
				session,
				contact_id,
				CANARY_B,
				"reminder",
				sms_provider=provider,
				dnc_provider=_FixedDnc(listed=False),
				idempotency_key="retry-key-1",
			)
		assert first_message_id == second_message_id == "stub-message-id"
		assert len(provider.calls) == 1, "the provider must be called exactly once across both attempts"
	finally:
		_cleanup_cold_sms_events(contact_id)


def test_dispatch_sms_concurrent_same_idempotency_key_is_rejected_by_unique_constraint(canary_tenants, monkeypatch):
	"""A second PENDING insert under the same (client_id, idempotency_key)
	before the first has resolved to SENT must be rejected by
	uq_sms_dispatch_log_client_idempotency_key and surfaced as
	SmsDispatchNeedsReconciliationError, not a silent double-send."""
	import src.services.campaign_readiness_gate as gate_module

	monkeypatch.setattr(gate_module, "_local_hour", lambda tz_name: 14)
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE contacts SET phone = '+18135550100' WHERE contact_id = :cid"),
			{"cid": contact_id},
		)
	# Note: no engagement event is seeded here on purpose — the idempotency
	# lookup below short-circuits dispatch_sms() before evaluate_full_
	# readiness() is ever called, so engagement status is irrelevant to
	# this test (see dispatch_sms()'s own ordering).
	try:
		with get_owner_db_context() as owner_session:
			owner_session.execute(
				text(
					"INSERT INTO sms_dispatch_log "
					"(client_id, contact_id, inbound_sms_count_at_send, booked_appointment_id_at_send, "
					"status, idempotency_key) "
					"VALUES (:client_id, :contact_id, 1, NULL, 'PENDING', 'racing-key-1')"
				),
				{"client_id": CANARY_A, "contact_id": contact_id},
			)
			owner_session.commit()

		provider = _CountingSmsProvider()
		with get_db_context(client_id=CANARY_A) as session:
			with pytest.raises(SmsDispatchNeedsReconciliationError):
				dispatch_sms(
					session,
					contact_id,
					CANARY_A,
					"reminder",
					sms_provider=provider,
					dnc_provider=_FixedDnc(listed=False),
					idempotency_key="racing-key-1",
				)
		assert provider.calls == [], "provider must never be called while the prior attempt is unresolved"
	finally:
		_cleanup_cold_sms_events(contact_id)


def test_evaluate_campaign_readiness_not_subverted_by_temp_table_shadowing(canary_tenants):
	"""PR #8 review: SECURITY DEFINER + SET search_path = public + unqualified
	table names (contacts/companies/client_pm_books/events) is a
	privilege-escalation hole — blackink_app can create temp tables (Postgres
	grants CREATE TEMP to PUBLIC by default), and Postgres searches a
	session's temp schema before any schema literally named in search_path.
	Without pg_temp explicitly listed (and last), a session-local `CREATE
	TEMP TABLE contacts (...)` would silently shadow the real table for this
	SECURITY DEFINER function, turning an app-role credential into a way to
	feed the owner-privileged function fabricated data (or divert its
	writes).

	This seeds a REAL non-poach match (so a correct, unsubverted run returns
	SUPPRESSED — a positive assertion, not just "didn't crash"), then
	creates lookalike temp contacts/companies/client_pm_books/events tables
	in the same session that would flip the result to PERMANENTLY_BLOCKED
	(via a lying is_opted_out) and hide the audit event, if the function
	were still resolving unqualified names against pg_temp first."""
	contact_id = canary_tenants[CANARY_A]["contact_id"]
	domain = canary_tenants[CANARY_A]["domain"]

	with get_db_context(client_id=CANARY_B) as session:
		session.execute(
			text("INSERT INTO public.client_pm_books (client_id, owner_domain) VALUES (:cid, :domain)"),
			{"cid": CANARY_B, "domain": domain},
		)
	try:
		with get_db_context(client_id=CANARY_A) as session:
			session.execute(
				text(
					"CREATE TEMP TABLE contacts (contact_id BIGINT PRIMARY KEY, "
					"company_id VARCHAR(64), email VARCHAR(255), "
					"is_opted_out BOOLEAN, suppression_state BOOLEAN)"
				)
			)
			session.execute(
				text(
					"INSERT INTO contacts VALUES "
					"(:cid, 'shadow-company', 'shadow@example.com', TRUE, FALSE)"
				),
				{"cid": contact_id},
			)
			session.execute(
				text("CREATE TEMP TABLE companies (company_id VARCHAR(64) PRIMARY KEY, domain VARCHAR(255))")
			)
			session.execute(text("INSERT INTO companies VALUES ('shadow-company', 'shadow-domain.example')"))
			session.execute(
				text(
					"CREATE TEMP TABLE client_pm_books (client_id VARCHAR(40), "
					"owner_domain VARCHAR(255), owner_email VARCHAR(255))"
				)
			)
			session.execute(text("CREATE TEMP TABLE events (client_id VARCHAR(40))"))

			status = session.execute(
				text("SELECT evaluate_campaign_readiness(:cid)"), {"cid": contact_id}
			).scalar()
			event = session.execute(
				text(
					"SELECT payload FROM public.events WHERE event_type = 'non_poach_suppressed' "
					"AND entity_id = :cid ORDER BY created_at DESC LIMIT 1"
				),
				{"cid": str(contact_id)},
			).first()

			# Drop the shadows explicitly before this connection goes back to
			# the pool — SQLAlchemy reuses physical connections across
			# get_db_context() calls, and a Postgres temp table lives for the
			# life of the BACKEND CONNECTION, not the `with` block. Left in
			# place, these would silently follow whichever test next happens
			# to check out this same pooled connection.
			session.execute(text("DROP TABLE pg_temp.contacts, pg_temp.companies, pg_temp.client_pm_books, pg_temp.events"))

		assert status == "SUPPRESSED", (
			"evaluate_campaign_readiness was subverted by session-local temp tables "
			"shadowing contacts/companies — a vulnerable version would return "
			"PERMANENTLY_BLOCKED here, reading the shadow's fabricated is_opted_out"
		)
		assert event is not None, (
			"non_poach_suppressed event was not found in the real public.events table "
			"— it may have been silently redirected into the temp events shadow"
		)
		assert event.payload["matched_client_id"] == CANARY_B
	finally:
		# Schema-qualified, unlike the other tests' identical cleanup above —
		# this test in particular may have left temp lookalikes of these same
		# table names on a pooled connection (see the DROP above); qualifying
		# here means this cleanup is correct regardless of whether that DROP
		# ran (e.g. an assertion failed first) or which pooled connection
		# these get_db_context() calls happen to reuse.
		with get_db_context(client_id=CANARY_B) as session:
			session.execute(text("DELETE FROM public.client_pm_books WHERE client_id = :cid"), {"cid": CANARY_B})
		with get_owner_db_context() as session:
			session.execute(
				text("DELETE FROM public.events WHERE event_type = 'non_poach_suppressed' AND entity_id = :cid"),
				{"cid": str(contact_id)},
			)
			session.commit()


# ── Subtask 1.1.1 — appointment ops: PR #25 review fixes ──
#
# Finding 1 (cross-tenant appointment records can be linked together): a bare
# appointment_id FK on the child tables only proves the parent row exists, not
# that it belongs to the same tenant. Proves the composite (client_id,
# appointment_id) FK — and the trigger's company_id/contact_id ownership
# check — actually reject a cross-tenant link, not just that the DDL parses.
#
# Finding 2 (state-machine rules unenforced): proves
# appointments_guard_transition() rejects reschedule_count > 2, a decrease in
# reschedule_count, and any change to opportunity_id — regardless of whether
# any caller goes through src/services/appointment_state.py.


def _insert_appointment(session, *, client_id, company_id, contact_id, opportunity_id=None):
	import uuid

	row = session.execute(
		text(
			"INSERT INTO appointments "
			"(client_id, company_id, contact_id, opportunity_id, scheduled_for, owner_brief_url) "
			"VALUES (:client_id, :company_id, :contact_id, :opportunity_id, NOW() + interval '1 day', "
			"'https://example.test/brief') "
			"RETURNING appointment_id"
		),
		{
			"client_id": client_id,
			"company_id": company_id,
			"contact_id": contact_id,
			"opportunity_id": opportunity_id or str(uuid.uuid4()),
		},
	).first()
	return row.appointment_id


def _delete_appointment(appointment_id):
	# DELETE is REVOKEd from both runtime roles on appointments (status-
	# transitioned, never removed at runtime — see apply_appointment_ops.py),
	# so cleanup must go through the owner role, not a tenant-scoped session.
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM confirmation_logs WHERE appointment_id = :aid"), {"aid": appointment_id})
		session.execute(text("DELETE FROM appointment_disputes WHERE appointment_id = :aid"), {"aid": appointment_id})
		session.execute(text("DELETE FROM appointment_dispositions WHERE appointment_id = :aid"), {"aid": appointment_id})
		session.execute(text("DELETE FROM appointments WHERE appointment_id = :aid"), {"aid": appointment_id})
		session.commit()


def test_confirmation_log_cannot_reference_another_tenants_appointment(canary_tenants):
	"""Composite FK proof: a Tenant-A session may write a confirmation_logs
	row with its own client_id, but must not be able to point it at an
	appointment that actually belongs to Tenant B."""
	with get_db_context(client_id=CANARY_B) as session:
		other_appointment_id = _insert_appointment(
			session,
			client_id=CANARY_B,
			company_id=canary_tenants[CANARY_B]["company_id"],
			contact_id=canary_tenants[CANARY_B]["contact_id"],
		)
	try:
		raised = False
		try:
			with get_db_context(client_id=CANARY_A) as session:
				session.execute(
					text(
						"INSERT INTO confirmation_logs "
						"(client_id, appointment_id, channel, confirmation_tier, sent_at, delivery_status) "
						"VALUES (:client_id, :appointment_id, 'EMAIL', '24H', NOW(), 'SENT')"
					),
					{"client_id": CANARY_A, "appointment_id": other_appointment_id},
				)
		except IntegrityError:
			raised = True
		assert raised, (
			"Tenant A inserted a confirmation_logs row referencing Tenant B's "
			"appointment_id — composite (client_id, appointment_id) FK did not reject it"
		)
	finally:
		_delete_appointment(other_appointment_id)


def test_appointment_dispute_cannot_reference_another_tenants_appointment(canary_tenants):
	with get_db_context(client_id=CANARY_A) as session:
		other_appointment_id = _insert_appointment(
			session,
			client_id=CANARY_A,
			company_id=canary_tenants[CANARY_A]["company_id"],
			contact_id=canary_tenants[CANARY_A]["contact_id"],
		)
	try:
		raised = False
		try:
			with get_db_context(client_id=CANARY_B) as session:
				session.execute(
					text(
						"INSERT INTO appointment_disputes (client_id, appointment_id, reason) "
						"VALUES (:client_id, :appointment_id, 'test')"
					),
					{"client_id": CANARY_B, "appointment_id": other_appointment_id},
				)
		except IntegrityError:
			raised = True
		assert raised, (
			"Tenant B inserted an appointment_disputes row referencing Tenant A's "
			"appointment_id — composite (client_id, appointment_id) FK did not reject it"
		)
	finally:
		_delete_appointment(other_appointment_id)


def test_appointment_cannot_be_booked_against_another_tenants_company(canary_tenants):
	"""Trigger proof: company_id has no static composite FK (companies is a
	shared, reassignable pool), so appointments_guard_transition() must reject
	an appointment whose client_id doesn't match the company's own
	owning_client_id."""
	raised = False
	try:
		with get_db_context(client_id=CANARY_A) as session:
			_insert_appointment(
				session,
				client_id=CANARY_A,
				company_id=canary_tenants[CANARY_B]["company_id"],
				contact_id=None,
			)
	except Exception:
		raised = True
	assert raised, (
		"Tenant A booked an appointment against Tenant B's company_id — "
		"appointments_guard_transition() did not reject the ownership mismatch"
	)


def test_appointment_reschedule_count_cannot_exceed_two(canary_tenants):
	with get_db_context(client_id=CANARY_A) as session:
		appointment_id = _insert_appointment(
			session,
			client_id=CANARY_A,
			company_id=canary_tenants[CANARY_A]["company_id"],
			contact_id=canary_tenants[CANARY_A]["contact_id"],
		)
	try:
		raised = False
		try:
			with get_db_context(client_id=CANARY_A) as session:
				session.execute(
					text("UPDATE appointments SET reschedule_count = 3 WHERE appointment_id = :aid"),
					{"aid": appointment_id},
				)
		except Exception:
			raised = True
		assert raised, "reschedule_count=3 was accepted — the trigger did not enforce the cap of 2"
	finally:
		_delete_appointment(appointment_id)


def test_appointment_opportunity_id_is_immutable(canary_tenants):
	import uuid

	with get_db_context(client_id=CANARY_A) as session:
		appointment_id = _insert_appointment(
			session,
			client_id=CANARY_A,
			company_id=canary_tenants[CANARY_A]["company_id"],
			contact_id=canary_tenants[CANARY_A]["contact_id"],
		)
	try:
		raised = False
		try:
			with get_db_context(client_id=CANARY_A) as session:
				session.execute(
					text("UPDATE appointments SET opportunity_id = :new_id WHERE appointment_id = :aid"),
					{"new_id": str(uuid.uuid4()), "aid": appointment_id},
				)
		except Exception:
			raised = True
		assert raised, "opportunity_id was changed — the trigger did not enforce immutability"
	finally:
		_delete_appointment(appointment_id)
