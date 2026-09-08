"""E2E sim for the 3.1.3 inbound reply bridge (local signed-POST).

Drives the real webhook (src.api.inbound_email_router) via FastAPI TestClient
with Mailgun-signed payloads, using the MAILGUN_SIGNING_KEY from the loaded env.
The full app code runs: HMAC verify -> inbound_ingest (resolve/dedup/attribute/
persist) -> real #sales-replies Slack card. Not mocked — cards actually post.

Seeds a clearly-marked test tenant (client_id='e2e_demo'), runs seven scenarios
(forged, unknown alias, bcc echo, tier-1, tier-2, unattributed, duplicate),
verifies the DB, then DELETES everything it seeded unless --keep is passed.

Run:  ENV_FILE=.env.test PYTHONPATH=. python scripts/e2e_inbound_bridge.py
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import time
import uuid

from contextlib import contextmanager

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from config.settings import get_settings
from src.api.inbound_email_router import router

_engine = None


@contextmanager
def get_system_db_context():
    """Superuser connection for seed/verify/cleanup — the harness touches
    reference + tenant tables (counties FK, events) that the blackink_system
    role isn't GRANTed on. The webhook flow under test still uses the app's
    own system context internally; this is only for test scaffolding."""
    global _engine
    if _engine is None:
        _engine = create_engine(get_settings().database_url)
    conn = _engine.connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


CLIENT_ID = "e2e_demo"
ALIAS = f"{CLIENT_ID}@inbound.getblackink.com"
DOMAIN = "e2edemo.example.com"
CONTACT_EMAIL = "e2e.prospect@example.com"
TOUCH1_MSGID = "<e2e-touch1@mail.example.com>"


def _company_id() -> str:
    return hashlib.sha256(DOMAIN.encode()).hexdigest()


def _sign(ts: str, token: str, key: str) -> str:
    return hmac.new(key.encode(), (ts + token).encode(), hashlib.sha256).hexdigest()


def _form(key: str, *, sender, recipient, subject, body, in_reply_to, message_id, forge=False):
    ts = str(int(time.time()))
    token = uuid.uuid4().hex
    sig = _sign(ts, token, "WRONG-KEY" if forge else key)
    headers = [["Message-Id", message_id]]
    if in_reply_to:
        headers.append(["In-Reply-To", in_reply_to])
    return {
        "timestamp": ts, "token": token, "signature": sig,
        "from": sender, "recipient": recipient, "subject": subject,
        "body-plain": body, "message-headers": json.dumps(headers),
    }


def seed():
    cid = _company_id()
    with get_system_db_context() as s:
        s.execute(text("INSERT INTO clients (client_id, display_name, is_active) "
                       "VALUES (:c,'E2E Demo',TRUE) ON CONFLICT (client_id) DO NOTHING"), {"c": CLIENT_ID})
        s.execute(text("INSERT INTO companies (company_id, company_name, domain, county_slug, owning_client_id) "
                       "VALUES (:id,'E2E Demo PM',:d,'hillsborough_fl',:c) ON CONFLICT (company_id) DO NOTHING"),
                  {"id": cid, "d": DOMAIN, "c": CLIENT_ID})
        contact_id = s.execute(text(
            "INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, email, phone) "
            "VALUES (:id,'OWNER_BROKER_MD','Jane','Prospect',:e,'+18135559999') RETURNING contact_id"),
            {"id": cid, "e": CONTACT_EMAIL}).scalar()
        run_id = s.execute(text("INSERT INTO sequence_runs (client_id, contact_id, status) "
                                "VALUES (:c,:ct,'ACTIVE') RETURNING run_id"),
                           {"c": CLIENT_ID, "ct": contact_id}).scalar()
        s.execute(text("INSERT INTO sequence_touch_dispatches (client_id, run_id, touch_step, status, message_id) "
                       "VALUES (:c,:r,1,'SENT',:m)"),
                  {"c": CLIENT_ID, "r": run_id, "m": TOUCH1_MSGID})
        s.commit()
    print(f"seeded: client={CLIENT_ID} contact_id={contact_id} run_id={run_id} touch1_msgid={TOUCH1_MSGID}")


def run_scenarios(client, key):
    results = []

    def post(name, **kw):
        r = client.post("/api/v1/webhooks/inbound-email", data=_form(key, **kw))
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        results.append((name, r.status_code, body))
        print(f"  {name:22} -> {r.status_code} {body}")
        return body

    print("scenarios:")
    post("forged_sig", sender="x@y.com", recipient=ALIAS, subject="hi", body="b",
         in_reply_to=None, message_id=f"<f-{uuid.uuid4()}@x>", forge=True)
    post("unknown_alias", sender="x@y.com", recipient="nobody@inbound.getblackink.com",
         subject="hi", body="b", in_reply_to=None, message_id=f"<u-{uuid.uuid4()}@x>")
    post("bcc_echo", sender="x@y.com", recipient=ALIAS, subject="echo", body="b",
         in_reply_to=None, message_id=TOUCH1_MSGID)
    post("attributed_tier1", sender="Jane Prospect <somewhere@else.com>", recipient=ALIAS,
         subject="Re: Your report", body="Yes interested", in_reply_to=TOUCH1_MSGID,
         message_id=f"<t1-{uuid.uuid4()}@x>")
    post("attributed_tier2", sender=f"Jane Prospect <{CONTACT_EMAIL}>", recipient=ALIAS,
         subject="Hello", body="tell me more", in_reply_to=None, message_id=f"<t2-{uuid.uuid4()}@x>")
    dup_id = f"<dup-{uuid.uuid4()}@x>"
    post("unattributed", sender="stranger@nowhere.com", recipient=ALIAS, subject="who",
         body="cold reply", in_reply_to=None, message_id=dup_id)
    post("duplicate", sender="stranger@nowhere.com", recipient=ALIAS, subject="who",
         body="cold reply", in_reply_to=None, message_id=dup_id)
    return results


def verify():
    with get_system_db_context() as s:
        rows = s.execute(text(
            "SELECT attribution_status, contact_id, run_id, from_address, subject "
            "FROM inbound_messages WHERE client_id=:c ORDER BY received_at"), {"c": CLIENT_ID}).mappings().all()
        ev = s.execute(text("SELECT COUNT(*) FROM events WHERE client_id=:c AND event_type='inbound_reply_received'"),
                       {"c": CLIENT_ID}).scalar()
    print("verify:")
    print(f"  inbound_messages rows: {len(rows)} (expect 3: tier1, tier2, unattributed)")
    for r in rows:
        print(f"    {r['attribution_status']:12} contact={r['contact_id']} run={str(r['run_id'])[:8] if r['run_id'] else None} from={r['from_address']}")
    print(f"  inbound_reply_received events: {ev} (expect 3)")
    statuses = sorted(r["attribution_status"] for r in rows)
    ok = len(rows) == 3 and statuses == ["attributed", "attributed", "unattributed"] and ev == 3
    print(f"  RESULT: {'PASS' if ok else 'CHECK'}")
    return ok


def cleanup():
    cid = _company_id()
    with get_system_db_context() as s:
        s.execute(text("DELETE FROM inbound_messages WHERE client_id=:c"), {"c": CLIENT_ID})
        s.execute(text("DELETE FROM events WHERE client_id=:c"), {"c": CLIENT_ID})
        s.execute(text("DELETE FROM sequence_touch_dispatches WHERE client_id=:c"), {"c": CLIENT_ID})
        s.execute(text("DELETE FROM sequence_runs WHERE client_id=:c"), {"c": CLIENT_ID})
        s.execute(text("DELETE FROM contacts WHERE company_id=:id"), {"id": cid})
        s.execute(text("DELETE FROM companies WHERE company_id=:id"), {"id": cid})
        s.execute(text("DELETE FROM clients WHERE client_id=:c"), {"c": CLIENT_ID})
        s.commit()
    print(f"cleanup: removed all e2e rows for client={CLIENT_ID}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="do NOT delete seeded data after the run")
    ap.add_argument("--cleanup-only", action="store_true", help="just delete e2e data and exit")
    args = ap.parse_args()

    if args.cleanup_only:
        cleanup()
        return

    key = get_settings().mailgun_signing_key
    if key is None:
        raise SystemExit("MAILGUN_SIGNING_KEY not set in env — cannot sign payloads")
    key = key.get_secret_value()

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    seed()
    run_scenarios(client, key)
    ok = verify()
    if args.keep:
        print("--keep set: leaving seeded data in place")
    else:
        cleanup()
    raise SystemExit(0 if ok else 1)


if __name__ == "__main__":
    main()
