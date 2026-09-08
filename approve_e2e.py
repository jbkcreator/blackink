"""Publish directly to relay:sends to simulate an approved campaign dispatch.

Run this after seed_e2e.py once the Cora worker logs 'approval_pending=1'.
"""
import logging, os
os.environ["ENV_FILE"] = ".env"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s — %(message)s")

from src.agents.relay import sends_queue as sq

WORK_ORDER_ID = "e2e-003"
COMPANY_ID    = "test-company-e2e"
CAMPAIGN_ID   = "test-campaign-e2e"
CLIENT_ID     = "test-client-e2e"

sq.ensure_group()
msg_id = sq.publish(
    work_order_id=WORK_ORDER_ID,
    campaign_id=CAMPAIGN_ID,
    company_id=COMPANY_ID,
    client_id=CLIENT_ID,
    draft_message_id="",
    pdf_url=None,
    video_id=None,
    landing_url=None,
    gif_url=None,
)
logging.info("Published to relay:sends message_id=%s work_order_id=%s", msg_id, WORK_ORDER_ID)
logging.info("Watch the Relay worker terminal now.")
