"""Ink Campaign Agent — HTTP webhooks.

POST /api/v1/webhooks/ink/approve
  Approves or rejects a pending campaign draft by publishing a resume signal
  to ink:resume_signals. The Ink worker picks it up and resumes wait_approve.

  This endpoint is the programmatic path (admin UI, automated tests). The
  normal operator path is the Slack card buttons, which call the Bolt action
  handlers in src/services/slack/listeners.py — those write to the same
  ink:resume_signals stream directly without going through HTTP.

  Auth: X-Ink-Webhook-Secret header matched against settings.ink_webhook_secret.
  Fail-closed: if the secret is unset, all requests are rejected (HTTP 503).
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from config.settings import get_settings
from src.core.redis_client import get_redis_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/webhooks/ink", tags=["ink-webhooks"])

_RESUME_STREAM_KEY = "ink:resume_signals"
_RESUME_GROUP_NAME = "ink_resume_workers"


def _ensure_resume_group() -> None:
    r = get_redis_client()
    try:
        r.xgroup_create(_RESUME_STREAM_KEY, _RESUME_GROUP_NAME, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


def publish_resume_signal(work_order_id: str, approved: bool, approved_by: str) -> None:
    """Write a resume signal to ink:resume_signals. Used by both this router
    and the Slack Bolt action handlers."""
    _ensure_resume_group()
    get_redis_client().xadd(_RESUME_STREAM_KEY, {
        "work_order_id":  work_order_id,
        "resume_payload": json.dumps({"approved": approved, "approved_by": approved_by}),
    })
    logger.info(
        "ink_webhook: resume signal published work_order_id=%s approved=%s by=%s",
        work_order_id, approved, approved_by,
    )


class ApproveRequest(BaseModel):
    work_order_id: str
    approved: bool
    approved_by: str


@router.post("/approve", status_code=200)
def approve_campaign(
    req: ApproveRequest,
    x_ink_webhook_secret: str = Header(default=""),
) -> dict:
    """Publish a resume signal so the Ink worker resumes past wait_approve."""
    settings = get_settings()

    if not settings.ink_webhook_secret:
        raise HTTPException(status_code=503, detail="Ink webhook secret not configured")

    expected = settings.ink_webhook_secret.get_secret_value()
    if not x_ink_webhook_secret or x_ink_webhook_secret != expected:
        logger.warning(
            "ink_webhook: rejected approve — bad secret work_order_id=%s",
            req.work_order_id,
        )
        raise HTTPException(status_code=401, detail="Invalid webhook secret")

    publish_resume_signal(req.work_order_id, req.approved, req.approved_by)
    return {"ok": True, "work_order_id": req.work_order_id, "approved": req.approved}
