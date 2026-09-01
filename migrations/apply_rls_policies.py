"""
Enable and FORCE Postgres Row-Level Security on every tenant-bearing table,
driven by config/tenant_policies.py (Dev 1 plan, migration 11 of 12).

RLS is the non-negotiable backstop of the tenant-isolation chassis — it
holds regardless of how a query was written (raw text() or ORM), unlike
app-layer-only enforcement (which is exactly what Forced Action's
venture_key demonstrates the failure mode of: an optional, unenforced,
unindexed-as-a-security-boundary column with zero leakage tests). See
docs/adr/0001-tenant-isolation-rls-plus-app-layer.md and the Dev 1 plan's
"Tenant isolation chassis" section.

Run LAST, after every tenant table exists (migrations 1-10 must already
be applied). Safely re-runnable: DROP POLICY IF EXISTS then recreate.

The verification step at the end queries pg_tables/pg_policies and FAILS
LOUDLY if any table registered in TENANT_POLICIES lacks
rowsecurity=true AND forcerowsecurity=true — this is what closes the exact
gap that let Forced Action ship with zero enforcement silently: a new
tenant-bearing table added later without being registered in
config/tenant_policies.py and re-run through this migration would be
caught here, not discovered in production.

    PYTHONPATH=. python migrations/apply_rls_policies.py
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from config.tenant_policies import TENANT_POLICIES
from src.core.database import get_owner_db_context


def _quote_ident(name: str) -> str:
	return '"' + name.replace('"', '""') + '"'


def _policy_sql(table: str, policy: dict) -> str:
	t = _quote_ident(table)
	if policy["mode"] == "direct":
		col = _quote_ident(policy["column"])
		condition = f"{t}.{col} = current_setting('app.current_client_id', true)"
	elif policy["mode"] == "join":
		join_table = _quote_ident(policy["join_table"])
		join_on = _quote_ident(policy["join_on"])
		join_col = _quote_ident(policy["join_column"])
		condition = (
			f"EXISTS (SELECT 1 FROM {join_table} j "
			f"WHERE j.{join_on} = {t}.{join_on} "
			f"AND j.{join_col} = current_setting('app.current_client_id', true))"
		)
	else:
		raise ValueError(f"Unknown tenant policy mode for {table!r}: {policy['mode']!r}")

	return (
		f"CREATE POLICY tenant_isolation ON {t} "
		f"USING ({condition}) WITH CHECK ({condition})"
	)


def main() -> int:
	with get_owner_db_context() as db:
		for table, policy in TENANT_POLICIES.items():
			t = _quote_ident(table)
			db.execute(text(f"ALTER TABLE {t} ENABLE ROW LEVEL SECURITY"))
			db.execute(text(f"ALTER TABLE {t} FORCE ROW LEVEL SECURITY"))
			db.execute(text(f"DROP POLICY IF EXISTS tenant_isolation ON {t}"))
			db.execute(text(_policy_sql(table, policy)))
		db.commit()

		# ── Fail-loud verification ──────────────────────────────────────────
		# pg_tables has no forcerowsecurity column — that flag lives on
		# pg_class.relforcerowsecurity, not the pg_tables view. rowsecurity
		# is available both places; queried from pg_class here too so both
		# columns come from one consistent source.
		rows = db.execute(
			text(
				"SELECT c.relname AS tablename, c.relrowsecurity AS rowsecurity, "
				"c.relforcerowsecurity AS forcerowsecurity "
				"FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
				"WHERE n.nspname = 'public' AND c.relname = ANY(:tables)"
			),
			{"tables": list(TENANT_POLICIES.keys())},
		).fetchall()
		by_table = {r.tablename: r for r in rows}

		problems = []
		for table in TENANT_POLICIES:
			row = by_table.get(table)
			if row is None:
				problems.append(f"{table}: table not found")
			elif not row.rowsecurity or not row.forcerowsecurity:
				problems.append(
					f"{table}: rowsecurity={row.rowsecurity}, forcerowsecurity={row.forcerowsecurity}"
				)

		policy_rows = db.execute(
			text(
				"SELECT tablename FROM pg_policies "
				"WHERE schemaname = 'public' AND policyname = 'tenant_isolation' "
				"AND tablename = ANY(:tables)"
			),
			{"tables": list(TENANT_POLICIES.keys())},
		).fetchall()
		tables_with_policy = {r.tablename for r in policy_rows}
		for table in TENANT_POLICIES:
			if table not in tables_with_policy:
				problems.append(f"{table}: tenant_isolation policy missing")

		if problems:
			print("apply_rls_policies: FAILED verification:", file=sys.stderr)
			for p in problems:
				print(f"  - {p}", file=sys.stderr)
			return 1

	print(f"apply_rls_policies: done — RLS enforced and verified on {len(TENANT_POLICIES)} tables")
	return 0


if __name__ == "__main__":
	raise SystemExit(main())
