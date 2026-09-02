"""Read-only view Looker Studio connects to for the Demo Friday Sandbox
dashboard (blueprint §3.1.6). A VIEW, not a new table — no tenant_policies
entry needed (it is a projection over already-registered tables, scoped by
a literal client_id filter baked into the view itself, not a fresh
tenant-bearing store).

SECURITY, not decoration — `WITH (security_invoker = true)` is mandatory
here: a normal Postgres view executes with its OWNER's privileges, so a
view created by the schema owner over RLS-protected tables and granted to
blackink_app would let blackink_app read rows RLS is supposed to filter.
security_invoker (PG 15+; CI runs postgres:16) makes the view run with the
QUERYING role's privileges instead, so RLS still applies through it. The
literal client_id filter below limits the blast radius to sandbox data
either way, but a view that structurally bypasses RLS is not a pattern to
leave lying around in a codebase whose core invariant is tenant isolation.
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = """
    CREATE OR REPLACE VIEW demo_sandbox_dashboard WITH (security_invoker = true) AS
    SELECT c.company_id, c.company_name, c.county_slug, c.door_count_est,
           c.current_pm_software, c.status,
           COUNT(e.id) FILTER (WHERE e.event_type = 'outbound_touch_dispatched') AS touches_sent,
           COUNT(e.id) FILTER (WHERE e.event_type = 'meeting_booked') AS meetings_booked
    FROM companies c
    LEFT JOIN events e ON e.entity_id = c.company_id AND e.client_id = 'DEMO_FRIDAY_SANDBOX'
    WHERE c.owning_client_id = 'DEMO_FRIDAY_SANDBOX'
    GROUP BY c.company_id
"""


def main() -> int:
    with get_owner_db_context() as db:
        db.execute(text(DDL))
        db.execute(text("GRANT SELECT ON demo_sandbox_dashboard TO blackink_app"))
        db.commit()
    print("apply_sandbox_dashboard_view: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
