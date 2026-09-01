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

from sqlalchemy import text

from config.tenant_policies import TENANT_POLICIES
from src.core.database import Database, get_db_context
from tests.fixtures.synthetic_tenants import CANARY_A, CANARY_B, canary_tenants  # noqa: F401


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
				text("SELECT is_claimed_by_other_client(:company_id, :client_id) AS claimed"),
				{"company_id": canary_tenants[CANARY_A]["company_id"], "client_id": CANARY_A},
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
