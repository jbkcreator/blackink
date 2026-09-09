"""Seed an Ink E2E test by injecting directly into the Cora queue.

Bypasses LangGraph's PostgresSaver entirely — no DB connection needed.

Usage:
    python seed_e2e.py
"""
import json, logging, os, time
os.environ["ENV_FILE"] = ".env"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")

from src.agents.cora import queue as cora_queue
from src.core.redis_client import get_redis_client

WORK_ORDER_ID = "e2e-003"
COMPANY_ID    = "test-company-e2e"
CAMPAIGN_ID   = "test-campaign-e2e"
CLIENT_ID     = "test-client-e2e"
LATENCY_SEC   = 51480    # 14 hr 18 min
LOSS_EST      = 23344    # $23,344

# ── 1. Publish directly to Cora's queue ──────────────────────────────────────
logging.info("Publishing draft.requested to Cora queue work_order_id=%s", WORK_ORDER_ID)
cora_queue.ensure_group()
cora_queue.publish(
    event_type="draft.requested",
    client_id=CLIENT_ID,
    payload={
        "work_order_id": WORK_ORDER_ID,
        "campaign_id":   CAMPAIGN_ID,
        "company_id":    COMPANY_ID,
        "latency_sec":   LATENCY_SEC,
        "loss_est":      LOSS_EST,
    },
)
logging.info("Draft event queued — watch the Cora worker terminal now.")
logging.info("Cora will generate the draft and post a Slack approval card.")
logging.info("")
logging.info("Once the Cora worker logs 'approval_pending=1', run:")
logging.info("")
logging.info("  python approve_e2e.py")
logging.info("")
