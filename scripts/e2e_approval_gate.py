"""End-to-end approval-gate driver for the outbound sequencer (Task 3.1.1).

Proves the REAL human-in-the-loop path: enroll -> approval card in Slack ->
human clicks Approve -> work-order sweep -> touch dispatched (SENDING -> SENT
with a Message-ID) -> event logged. The email SENDER is still the stub
(mints a Message-ID, transmits nothing) — everything up to "would send" is
exercised for real. Swap StubEmailSender for a real SMTP sender and this same
flow delivers actual mail with no other change.

All rows use a distinguishing client_id / domain so teardown is exact. Never
point this at a DB holding real prospect data without changing _CLIENT_ID.

Prereqs:
  - migrations applied (through apply_rls_policies.py)
  - .env has the test Slack creds + channels + BLACKINK_GLOBAL_APPROVERS
  - for the `approve` step to be clickable, the Bolt socket-mode listener must
    be running:  uvicorn src.api.main:app   (needs `pip install -r requirements.txt`)

Usage (run in order):
  PYTHONPATH=. python scripts/e2e_approval_gate.py seed
  PYTHONPATH=. python scripts/e2e_approval_gate.py enroll
  PYTHONPATH=. python scripts/e2e_approval_gate.py post-cards
  #   -> card appears in #blackink-setter; click Approve as an authorized user
  PYTHONPATH=. python scripts/e2e_approval_gate.py execute
  PYTHONPATH=. python scripts/e2e_approval_gate.py verify
  PYTHONPATH=. python scripts/e2e_approval_gate.py teardown

Shortcut for the non-Slack path (skip the human click by approving in SQL):
  PYTHONPATH=. python scripts/e2e_approval_gate.py approve-sql
"""

import sys

sys.path.insert(0, ".")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from sqlalchemy import text

from src.core.database import get_db_context, get_owner_db_context, get_system_db_context
from src.loaders.base import BaseIngestLoader

_CLIENT_ID = "_e2e_approval"
_COUNTY = "hillsborough_fl"
_DOMAIN = "acme-e2e.test"                # prospect company domain
_SENDING_DOMAIN = "outreach-e2e.test"    # our warmed sending domain
_MAILBOX = "rep@outreach-e2e.test"
_CONTACT_EMAIL = "owner@acme-e2e.test"


def seed() -> int:
    """Create client + warmed sending domain + warmed mailbox + company +
    a compliance-clean contact (no phone -> DNC PASS, VERIFIED, eligible)."""
    company_id = BaseIngestLoader.compute_company_id(_DOMAIN)
    with get_system_db_context() as s:
        s.execute(
            text("INSERT INTO clients (client_id, display_name, is_active) "
                 "VALUES (:c, :c, TRUE) ON CONFLICT (client_id) DO NOTHING"),
            {"c": _CLIENT_ID},
        )
        domain_id = s.execute(
            text("INSERT INTO sending_domains "
                 "(domain, client_id, warmup_status, quarantine_state, "
                 " spf_validated, dkim_validated, dmarc_validated) "
                 "VALUES (:d, :c, 'warmed', 'active', TRUE, TRUE, TRUE) "
                 "ON CONFLICT (domain) DO UPDATE SET warmup_status='warmed', quarantine_state='active' "
                 "RETURNING id"),
            {"d": _SENDING_DOMAIN, "c": _CLIENT_ID},
        ).scalar()
        s.execute(
            text("INSERT INTO mailboxes "
                 "(domain_id, mailbox_address, client_id, warmup_status, quarantine_state) "
                 "VALUES (:did, :m, :c, 'warmed', 'active') "
                 "ON CONFLICT (mailbox_address) DO UPDATE SET warmup_status='warmed'"),
            {"did": domain_id, "m": _MAILBOX, "c": _CLIENT_ID},
        )
        s.execute(
            text("INSERT INTO companies "
                 "(company_id, company_name, domain, county_slug, owning_client_id) "
                 "VALUES (:cid, :name, :dom, :county, :c) "
                 "ON CONFLICT (company_id) DO NOTHING"),
            {"cid": company_id, "name": "Acme E2E PM", "dom": _DOMAIN,
             "county": _COUNTY, "c": _CLIENT_ID},
        )
        contact_id = s.execute(
            text("INSERT INTO contacts "
                 "(company_id, contact_role_type, email, email_status, "
                 " is_opted_out, suppression_state, dnc_clean, compliance_eligibility) "
                 "VALUES (:cid, 'OWNER_BROKER_MD', :email, 'VERIFIED', "
                 " FALSE, FALSE, TRUE, 'EMAIL_COLD_ELIGIBLE') "
                 "ON CONFLICT (company_id, contact_role_type) DO UPDATE "
                 "  SET email_status='VERIFIED', compliance_eligibility='EMAIL_COLD_ELIGIBLE' "
                 "RETURNING contact_id"),
            {"cid": company_id, "email": _CONTACT_EMAIL},
        ).scalar()
    print(f"seed: client={_CLIENT_ID} domain_id={domain_id} contact_id={contact_id} "
          f"mailbox={_MAILBOX}")
    return 0


def _contact_id() -> int:
    company_id = BaseIngestLoader.compute_company_id(_DOMAIN)
    with get_system_db_context() as s:
        return s.execute(
            text("SELECT contact_id FROM contacts WHERE company_id = :cid"),
            {"cid": company_id},
        ).scalar()


def enroll() -> int:
    from src.services.sequence_enrollment import enroll_contact
    cid = _contact_id()
    with get_db_context(client_id=_CLIENT_ID) as s:
        run_id = enroll_contact(s, _CLIENT_ID, cid, _CONTACT_EMAIL)
    print(f"enroll: contact_id={cid} run_id={run_id} "
          f"({'enrolled' if run_id else 'already active — skipped'})")
    return 0


def post_cards() -> int:
    from src.tasks.sequence_sweep import run_sweep
    posted = run_sweep(client_id=_CLIENT_ID)
    print(f"post-cards: {posted} approval card(s) posted to #blackink-setter. "
          f"Click Approve as an authorized user, then run `execute`.")
    return 0


def approve_sql() -> int:
    """Skip the human Slack click — flip the due Touch-1 order to APPROVED in
    SQL. Only for the non-Slack test path; the real gate is the button."""
    with get_db_context(client_id=_CLIENT_ID) as s:
        rows = s.execute(
            text("UPDATE agent_work_orders SET status='APPROVED', updated_at=NOW() "
                 "WHERE client_id=:c AND action_class='DISPATCH_EMAIL_TOUCH' "
                 "  AND status='QUEUED' AND due_at <= NOW() "
                 "RETURNING action_id"),
            {"c": _CLIENT_ID},
        ).fetchall()
    print(f"approve-sql: {len(rows)} order(s) forced APPROVED (bypassed Slack). Run `execute`.")
    return 0


def execute() -> int:
    from src.services.work_orders.__main__ import cmd_sweep
    return cmd_sweep(_CLIENT_ID)


def verify() -> int:
    with get_db_context(client_id=_CLIENT_ID) as s:
        disp = s.execute(
            text("SELECT touch_step, status, message_id FROM sequence_touch_dispatches "
                 "WHERE client_id=:c ORDER BY touch_step"),
            {"c": _CLIENT_ID},
        ).fetchall()
        evts = s.execute(
            text("SELECT event_type, entity_id, actor, payload FROM events "
                 "WHERE client_id=:c AND event_type='outbound_touch_dispatched'"),
            {"c": _CLIENT_ID},
        ).fetchall()
        orders = s.execute(
            text("SELECT action_class, status FROM agent_work_orders "
                 "WHERE client_id=:c ORDER BY due_at"),
            {"c": _CLIENT_ID},
        ).fetchall()
    print("verify:")
    print("  dispatches:", [(d.touch_step, d.status, bool(d.message_id)) for d in disp])
    print("  events:", [(e.event_type, e.entity_id, e.actor) for e in evts])
    print("  work_orders:", [(o.action_class, o.status) for o in orders])
    ok = any(d.status == "SENT" and d.message_id for d in disp) and len(evts) >= 1
    print("  RESULT:", "PASS — touch SENT + event logged" if ok
          else "not yet SENT (approve a card + run execute)")
    return 0 if ok else 1


def teardown() -> int:
    company_id = BaseIngestLoader.compute_company_id(_DOMAIN)
    with get_owner_db_context() as s:
        s.execute(text("DELETE FROM sequence_touch_dispatches WHERE client_id=:c"), {"c": _CLIENT_ID})
        s.execute(text("DELETE FROM sequence_runs WHERE client_id=:c"), {"c": _CLIENT_ID})
        s.execute(text("DELETE FROM agent_work_orders WHERE client_id=:c"), {"c": _CLIENT_ID})
        s.execute(text("DELETE FROM events WHERE client_id=:c"), {"c": _CLIENT_ID})
        s.execute(text("DELETE FROM compliance_gate_checks WHERE client_id=:c"), {"c": _CLIENT_ID})
        s.execute(text("DELETE FROM contacts WHERE company_id=:cid"), {"cid": company_id})
        s.execute(text("DELETE FROM companies WHERE company_id=:cid"), {"cid": company_id})
        s.execute(text("DELETE FROM mailboxes WHERE client_id=:c"), {"c": _CLIENT_ID})
        s.execute(text("DELETE FROM sending_domains WHERE client_id=:c"), {"c": _CLIENT_ID})
        s.execute(text("DELETE FROM clients WHERE client_id=:c"), {"c": _CLIENT_ID})
    print(f"teardown: removed all rows for client={_CLIENT_ID}")
    return 0


_CMDS = {
    "seed": seed, "enroll": enroll, "post-cards": post_cards,
    "approve-sql": approve_sql, "execute": execute, "verify": verify,
    "teardown": teardown,
}


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in _CMDS:
        print("usage: e2e_approval_gate.py {" + "|".join(_CMDS) + "}")
        return 2
    return _CMDS[sys.argv[1]]()


if __name__ == "__main__":
    raise SystemExit(main())
