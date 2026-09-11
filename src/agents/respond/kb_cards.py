"""Slack Block Kit cards for KB auto-response approval (Subtask 2.1.3).

Three card variants, all posted to #sales-replies (channel_key="replies"):

  HIGH_CONFIDENCE  — match_confidence >= 0.90
      Shows inbound message + draft response + Approve / Edit buttons.
      Approve click → email dispatched via approve_kb_response listener.

  LOW_CONFIDENCE   — match found but confidence < 0.90
      Shows ":warning: Low confidence — review required" banner, same
      body layout. No Approve button. Manual reply only.

  NO_MATCH         — no KB entry cleared its threshold
      Plain notice. requires_human_review=TRUE on the DB row.

Card hash for the Approve button:
    SHA-256(db_id, client_id, entry_id, card_posted_at_iso)
so a tampered or replayed Approve click is rejected.

Public API:
    compute_kb_card_hash()   — called from the listener to verify clicks
    post_kb_card()           — async, called from the worker via asyncio.run()
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text

from src.agents.respond.kb_matcher import KbMatch
from src.core.database import get_system_db_context
from src.services.events import log_event
from src.services.slack import post as slack_post

logger = logging.getLogger(__name__)

KB_CARD_EXPIRY = timedelta(hours=24)


def compute_kb_card_hash(db_id: int, client_id: str, entry_id: int, card_posted_at_iso: str) -> str:
    raw = json.dumps(
        {"db_id": db_id, "client_id": client_id, "entry_id": entry_id, "card_posted_at": card_posted_at_iso},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def _fetch_message(db, db_id: int) -> dict:
    row = db.execute(
        text("SELECT body_text, subject, sender_email, sender_name FROM inbound_messages WHERE id = :id"),
        {"id": db_id},
    ).first()
    if not row:
        return {}
    return {
        "body_text":    row[0] or "",
        "subject":      row[1] or "",
        "sender_email": row[2] or "",
        "sender_name":  row[3] or "",
    }


def _build_high_confidence_blocks(
    *,
    db_id: int,
    client_id: str,
    sender_name: str,
    sender_email: str,
    subject: str,
    body_text: str,
    kb_match: KbMatch,
    hash_value: str,
) -> list[dict]:
    confidence_pct = f"{kb_match.match_confidence * 100:.0f}%"
    snippet = body_text[:400].replace("\n", "\n> ")
    draft_preview = kb_match.response_template[:500]

    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f":speech_balloon: QUESTION — {sender_name or sender_email}", "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":green_circle: KB match: *{kb_match.topic}* — confidence {confidence_pct}"}],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Their message*\n> {snippet}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Draft response (awaiting approval)*\n{draft_preview}"},
        },
        {"type": "divider"},
    ]

    btn_value = json.dumps(
        {"db_id": db_id, "client_id": client_id, "entry_id": kb_match.entry_id, "card_hash": hash_value},
        separators=(",", ":"),
    )
    blocks.append({
        "type": "actions",
        "elements": [
            {
                "type": "button",
                "style": "primary",
                "text": {"type": "plain_text", "text": "Approve & Send", "emoji": True},
                "action_id": "approve_kb_response",
                "value": btn_value,
            },
            {
                "type": "button",
                "text": {"type": "plain_text", "text": "Mark reviewed", "emoji": True},
                "action_id": "kb_mark_reviewed",
                "value": json.dumps({"db_id": db_id, "client_id": client_id}, separators=(",", ":")),
            },
        ],
    })
    return blocks


def _build_low_confidence_blocks(
    *,
    sender_name: str,
    sender_email: str,
    body_text: str,
    kb_match: KbMatch,
    db_id: int,
    client_id: str,
) -> list[dict]:
    confidence_pct = f"{kb_match.match_confidence * 100:.0f}%"
    snippet = body_text[:400].replace("\n", "\n> ")

    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f":speech_balloon: QUESTION — {sender_name or sender_email}", "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f":warning: *Low confidence — review required* | closest match: *{kb_match.topic}* ({confidence_pct})"}],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Their message*\n> {snippet}"},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "Mark reviewed", "emoji": True},
                "action_id": "kb_mark_reviewed",
                "value": json.dumps({"db_id": db_id, "client_id": client_id}, separators=(",", ":")),
            }],
        },
    ]


def _build_no_match_blocks(
    *,
    sender_name: str,
    sender_email: str,
    body_text: str,
    db_id: int,
    client_id: str,
) -> list[dict]:
    snippet = body_text[:400].replace("\n", "\n> ")
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f":speech_balloon: QUESTION — {sender_name or sender_email}", "emoji": True},
        },
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": ":mag: *No KB match — manual reply needed*"}],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Their message*\n> {snippet}"},
        },
        {"type": "divider"},
        {
            "type": "actions",
            "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "Mark reviewed", "emoji": True},
                "action_id": "kb_mark_reviewed",
                "value": json.dumps({"db_id": db_id, "client_id": client_id}, separators=(",", ":")),
            }],
        },
    ]


async def post_kb_card(
    *,
    db_id: int,
    client_id: str,
    sender_email: str,
    kb_match: Optional[KbMatch],
) -> Optional[dict]:
    """Build and post the appropriate #sales-replies card.

    Returns {"card_ts", "card_channel_id", "card_posted_at_iso"} on success,
    None if Slack is unconfigured or posting fails.
    """
    try:
        with get_system_db_context() as db:
            msg = _fetch_message(db, db_id)

        sender_name  = msg.get("sender_name") or sender_email
        body_text    = msg.get("body_text", "")
        subject      = msg.get("subject", "")
        posted_at    = datetime.now(timezone.utc)
        posted_at_iso = posted_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        if kb_match and kb_match.match_confidence >= 0.90:
            hash_value = compute_kb_card_hash(db_id, client_id, kb_match.entry_id, posted_at_iso)
            blocks = _build_high_confidence_blocks(
                db_id=db_id,
                client_id=client_id,
                sender_name=sender_name,
                sender_email=sender_email,
                subject=subject,
                body_text=body_text,
                kb_match=kb_match,
                hash_value=hash_value,
            )
            fallback = f":speech_balloon: QUESTION from {sender_email} — KB draft ready for approval"
        elif kb_match:
            blocks = _build_low_confidence_blocks(
                sender_name=sender_name,
                sender_email=sender_email,
                body_text=body_text,
                kb_match=kb_match,
                db_id=db_id,
                client_id=client_id,
            )
            fallback = f":warning: QUESTION from {sender_email} — low confidence, manual review"
        else:
            blocks = _build_no_match_blocks(
                sender_name=sender_name,
                sender_email=sender_email,
                body_text=body_text,
                db_id=db_id,
                client_id=client_id,
            )
            fallback = f":mag: QUESTION from {sender_email} — no KB match, manual reply needed"

        slack_result = await slack_post.post_action_card(
            channel_key="replies",
            text=fallback,
            blocks=blocks,
        )

        log_event(
            client_id,
            "kb_card_posted",
            entity_type="inbound_message",
            entity_id=str(db_id),
            payload={
                "kb_match":         kb_match.topic if kb_match else None,
                "match_confidence": kb_match.match_confidence if kb_match else None,
                "card_variant":     (
                    "high_confidence" if (kb_match and kb_match.match_confidence >= 0.90)
                    else "low_confidence" if kb_match
                    else "no_match"
                ),
                "card_posted":      slack_result is not None,
            },
            actor="respond_worker",
        )

        if not slack_result:
            return None

        return {
            "card_ts":            slack_result["message_ts"],
            "card_channel_id":    slack_result["channel_id"],
            "card_posted_at_iso": posted_at_iso,
        }

    except Exception:
        logger.exception("kb_cards.post_kb_card: failed for db_id=%s client=%s", db_id, client_id)
        return None
