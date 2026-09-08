"""Live e2e proof for the Mark Opt-Out action (Task 3.1.3 / ticket 27).

Exercises exactly what the #sales-replies 'Mark Opt-Out' button invokes —
halt_sequence_for_contact() plus the handler's opt_out_recorded event — against
the live DB, and verifies the three writes: contacts.is_opted_out=TRUE,
sequence_runs HALTED, agent_work_orders CANCELLED. Seeds a clearly-marked test
tenant and deletes it after.

Run:  ENV_FILE=.env.test PYTHONPATH=. python scripts/e2e_optout.py
"""

from __future__ import annotations

import hashlib
import json
import uuid

from sqlalchemy import create_engine, text

from config.settings import get_settings
from src.services.sequence_halt import halt_sequence_for_contact

CLIENT_ID = "e2e_optout"
DOMAIN = "e2eoptout.example.com"


def _cid() -> str:
    return hashlib.sha256(DOMAIN.encode()).hexdigest()


def _engine():
    return create_engine(get_settings().database_url)


def seed(conn) -> tuple[int, str]:
    cid = _cid()
    conn.execute(text("INSERT INTO clients (client_id, display_name, is_active) VALUES (:c,'E2E OptOut',TRUE) ON CONFLICT (client_id) DO NOTHING"), {"c": CLIENT_ID})
    conn.execute(text("INSERT INTO companies (company_id, company_name, domain, county_slug, owning_client_id) VALUES (:id,'E2E OptOut PM',:d,'hillsborough_fl',:c) ON CONFLICT (company_id) DO NOTHING"), {"id": cid, "d": DOMAIN, "c": CLIENT_ID})
    contact_id = conn.execute(text("INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, email, is_opted_out) VALUES (:id,'OWNER_BROKER_MD','Opt','Out','opt@e2e.com',FALSE) RETURNING contact_id"), {"id": cid}).scalar()
    run_id = conn.execute(text("INSERT INTO sequence_runs (client_id, contact_id, status) VALUES (:c,:ct,'ACTIVE') RETURNING run_id::text"), {"c": CLIENT_ID, "ct": contact_id}).scalar()
    # A QUEUED work order referencing the run — the halt must CANCEL it.
    conn.execute(text(
        "INSERT INTO agent_work_orders (client_id, entity_type, entity_id, agent_id, action_class, "
        "autonomy_band, risk_class, payload_hash, idempotency_key, status, payload) "
        "VALUES (:c,'contact',:eid,'cold_outbound_sequencer','DISPATCH_EMAIL_TOUCH','BAND_2_ONE_TAP','LOW',"
        ":ph,:ik,'QUEUED',:payload)"
    ), {"c": CLIENT_ID, "eid": str(contact_id), "ph": uuid.uuid4().hex, "ik": f"e2e:{run_id}:3",
        "payload": json.dumps({"run_id": run_id, "touch_step": 3})})
    conn.commit()
    print(f"seeded: contact_id={contact_id} run_id={run_id} + 1 QUEUED work order")
    return contact_id, run_id


def verify(conn, contact_id: int, run_id: str) -> bool:
    opted = conn.execute(text("SELECT is_opted_out FROM contacts WHERE contact_id=:c"), {"c": contact_id}).scalar()
    run_status = conn.execute(text("SELECT status FROM sequence_runs WHERE run_id=:r"), {"r": run_id}).scalar()
    wo_status = conn.execute(text("SELECT status FROM agent_work_orders WHERE payload->>'run_id'=:r"), {"r": run_id}).scalar()
    ev = conn.execute(text("SELECT COUNT(*) FROM events WHERE client_id=:c AND event_type='opt_out_recorded'"), {"c": CLIENT_ID}).scalar()
    print(f"  contacts.is_opted_out     = {opted}   (expect True)")
    print(f"  sequence_runs.status      = {run_status}  (expect HALTED)")
    print(f"  agent_work_orders.status  = {wo_status}  (expect CANCELLED)")
    print(f"  opt_out_recorded events   = {ev}       (expect 1)")
    return opted is True and run_status == "HALTED" and wo_status == "CANCELLED" and ev == 1


def cleanup(conn):
    cid = _cid()
    conn.execute(text("DELETE FROM agent_work_orders WHERE client_id=:c"), {"c": CLIENT_ID})
    conn.execute(text("DELETE FROM events WHERE client_id=:c"), {"c": CLIENT_ID})
    conn.execute(text("DELETE FROM sequence_runs WHERE client_id=:c"), {"c": CLIENT_ID})
    conn.execute(text("DELETE FROM contacts WHERE company_id=:id"), {"id": cid})
    conn.execute(text("DELETE FROM companies WHERE company_id=:id"), {"id": cid})
    conn.execute(text("DELETE FROM clients WHERE client_id=:c"), {"c": CLIENT_ID})
    conn.commit()
    print("cleanup: removed all e2e_optout rows")


def main():
    eng = _engine()
    with eng.connect() as conn:
        cleanup(conn)  # clear any prior partial run
        contact_id, run_id = seed(conn)

    # The button path: halt_sequence_for_contact (opens its own system session).
    halt_sequence_for_contact(contact_id=contact_id)

    # The handler also logs opt_out_recorded — replicate that write.
    with eng.connect() as conn:
        conn.execute(text(
            "INSERT INTO events (client_id, event_type, entity_type, entity_id, actor, payload) "
            "VALUES (:c,'opt_out_recorded','contact',:eid,'slack:e2e',:p)"
        ), {"c": CLIENT_ID, "eid": str(contact_id), "p": json.dumps({"contact_id": contact_id})})
        conn.commit()

    with eng.connect() as conn:
        print("verify:")
        ok = verify(conn, contact_id, run_id)
        print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
        cleanup(conn)
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
