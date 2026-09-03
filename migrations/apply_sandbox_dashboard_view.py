"""Read-only view Looker Studio connects to for the Demo Friday Sandbox
dashboard (blueprint §3.1.6). A VIEW, not a new table — no tenant_policies
entry needed (it is a projection over already-registered tables, scoped by
a literal client_id filter baked into the view itself, not a fresh
tenant-bearing store).

DELIBERATELY NOT `security_invoker` — a narrow, documented exception, not
an oversight. The general risk security_invoker guards against is a view
whose result set depends on the QUERYING session's own context (a
caller-influenced query over RLS-protected tables run with the view
OWNER's privileges could be used to read rows RLS should have hidden).
This view has no such surface: its `WHERE owning_client_id =
'DEMO_FRIDAY_SANDBOX'` filter is a literal baked into the view's
definition at creation time, not a parameter or a function of the
connecting session — every query against this view, from any role granted
SELECT on it, returns exactly the same fixed row set (sandbox companies
only), regardless of who is asking or what tenant context (if any) their
session has. That is also precisely why it must NOT be security_invoker:
an external BI tool (Looker Studio) connects as `blackink_app` with no
`app.current_client_id` ever set for that session, and RLS's
`current_setting('app.current_client_id', true)` resolves to NULL with no
value set — which satisfies no row's equality check, so a security_invoker
version of this view would correctly enforce RLS and correctly return zero
rows to every caller, defeating the dashboard's only purpose. Running as
the view owner (the default, without security_invoker) sidesteps that
because the fixed WHERE clause is the only filter that ever applies here —
there is no tenant context to get wrong.

If this view is ever changed to accept a parameter, join a table without a
literal tenant filter, or otherwise let the caller influence which rows
come back, security_invoker must be reinstated and a real per-role tenant
context solved for instead (see PR review discussion for the
dedicated-role alternative that was considered and deferred as
unnecessary for this single, fixed-filter view).
"""
import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_owner_db_context

DDL = """
    CREATE OR REPLACE VIEW demo_sandbox_dashboard AS
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
        # Explicit, not assumed: CREATE OR REPLACE VIEW's exact handling of
        # a previously-set reloption (security_invoker was set on the first
        # deploy of this view, before this fix) is not something to guess
        # about for a security-relevant setting — RESET makes the outcome
        # certain regardless, and is a no-op if it was never set.
        db.execute(text("ALTER VIEW demo_sandbox_dashboard RESET (security_invoker)"))
        db.execute(text("GRANT SELECT ON demo_sandbox_dashboard TO blackink_app"))
        db.commit()
    print("apply_sandbox_dashboard_view: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
