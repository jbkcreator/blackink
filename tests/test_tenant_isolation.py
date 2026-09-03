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
from src.core.database import Database, get_db_context, get_owner_db_context, get_system_db_context
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
