"""1-Screen context cards for the Respond Reply Triage Agent (Subtask 2.1.2).

Posted to #blackink-setter immediately after HOT_LEAD, WHALE_OWNER, or
OBJECTION classification. Each card is a one-screen briefing — owner
profile, OVS score, engagement history, suggested opener, and (for
OBJECTION) the relevant playbook entry.

Card hash: SHA-256(db_id, client_id, intent, card_posted_at ISO string).
The Claim button carries this hash; the listener rejects clicks that are
expired (>24h from card_posted_at) or whose hash doesn't match.

All data lookups degrade gracefully — a missing contact, OVS row, or
engagement record omits that section rather than blocking the post.

Public API:
  CONTEXT_CARD_INTENTS — set of intents that trigger a card
  post_context_card()  — async, called from the worker via asyncio.run()
  compute_card_hash()  — called from the Slack listener to verify Claim clicks
"""
from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.agents.respond.intents import Intent
from src.core.database import get_system_db_context
from src.services.events import log_event
from src.services.slack import post as slack_post

logger = logging.getLogger(__name__)

# Intents that generate a context card (imported by the worker).
CONTEXT_CARD_INTENTS = {Intent.HOT_LEAD, Intent.WHALE_OWNER, Intent.OBJECTION}

# How long a posted card stays claimable.
CARD_EXPIRY = timedelta(hours=24)

# ── Signal display labels ─────────────────────────────────────────────────────

_SIGNAL_LABELS: dict[str, str] = {
    "owner_page":        "Owner Page",
    "contact_info":      "Contact Info",
    "tech_health":       "Tech Health",
    "after_hours":       "After-Hours Contact",
    "dbpr_licence":      "DBPR Licence",
    "google_rating":     "Google Rating",
    "review_volume":     "Review Volume",
    "review_recency":    "Review Recency",
    "profile_complete":  "Profile Completeness",
    "response_rate":     "Response Rate",
}

# ── Objection playbooks ───────────────────────────────────────────────────────

OBJECTION_PLAYBOOKS: dict[str, str] = {
    "pricing": (
        "*Pricing objection* — redirect to value gap, not fee defence.\n"
        "> \"Most owners who start with price realise the real cost is vacancy days and bad turns. "
        "What's your current vacancy rate looking like?\""
    ),
    "timing": (
        "*Timing objection* — validate the constraint, plant a seed, park it cleanly.\n"
        "> \"Completely understand — the last thing I'd want is to rush this decision. "
        "Can I send you our county performance report so you have something concrete when the timing is right?\""
    ),
    "existing_agency": (
        "*Existing agency objection* — don't attack the incumbent, ask about gaps.\n"
        "> \"That makes sense. Most owners who do switch tell me there was one specific thing that finally tipped it. "
        "Is there anything about your current setup you'd change if you could?\""
    ),
    "capacity": (
        "*Capacity objection* — owner feels portfolio is too small or they're not ready.\n"
        "> \"No pressure at all. We do reserve slots for expansions — if you pick up another door in the "
        "next 6 months, a quick 10-minute call now means you're not starting from scratch then.\""
    ),
    "other": (
        "*Objection detected — subtype unclear.* Review the thread and apply the closest playbook.\n"
        "General: acknowledge → clarify → redirect to outcome."
    ),
}

_INTENT_EMOJI = {
    Intent.HOT_LEAD:    "🔥",
    Intent.WHALE_OWNER: "🐳",
    Intent.OBJECTION:   "⚡",
}

# ── Card hash ─────────────────────────────────────────────────────────────────

def compute_card_hash(db_id: int, client_id: str, intent: str, card_posted_at_iso: str) -> str:
    """Deterministic hash binding a card to its identity at post time.
    Used by the Claim listener to reject tampered or stale button clicks."""
    raw = json.dumps(
        {"db_id": db_id, "client_id": client_id, "intent": intent, "card_posted_at": card_posted_at_iso},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode()).hexdigest()


# ── Data fetchers ─────────────────────────────────────────────────────────────

def _fetch_contact(db: Session, sender_email: str) -> Optional[dict[str, Any]]:
    row = db.execute(
        text(
            "SELECT c.contact_id, c.first_name, c.last_name, c.email, "
            "       co.company_name, co.door_count_est, co.county_slug, co.company_id "
            "FROM contacts c "
            "JOIN companies co ON co.company_id = c.company_id "
            "WHERE c.email = :email "
            "LIMIT 1"
        ),
        {"email": sender_email},
    ).mappings().first()
    return dict(row) if row else None


def _fetch_ovs(db: Session, company_id: Any) -> Optional[dict[str, Any]]:
    if not company_id:
        return None
    row = db.execute(
        text(
            "SELECT score_total, county_rank, county_slug, signal_detail "
            "FROM owner_visibility_scores "
            "WHERE company_id = :cid "
            "ORDER BY month_key DESC LIMIT 1"
        ),
        {"cid": str(company_id)},
    ).mappings().first()
    return dict(row) if row else None


def _fetch_prior_messages(db: Session, client_id: str, sender_email: str, exclude_id: int) -> list[dict]:
    rows = db.execute(
        text(
            "SELECT sender_email, body_text, received_at, intent "
            "FROM inbound_messages "
            "WHERE client_id = :cid AND sender_email = :email AND id != :exc "
            "ORDER BY received_at DESC LIMIT 3"
        ),
        {"cid": client_id, "email": sender_email, "exc": exclude_id},
    ).mappings().fetchall()
    return [dict(r) for r in rows]


def _fetch_engagement(db: Session, contact_id: Optional[int]) -> dict[str, Any]:
    if not contact_id:
        return {"touches": 0, "opens": "n/a", "clicks": "n/a"}
    touch_row = db.execute(
        text("SELECT COUNT(*) FROM sequence_touch_dispatches WHERE contact_id = :cid"),
        {"cid": contact_id},
    ).scalar()
    # Group D / D-4: email_opened/email_clicked have no producer anywhere in
    # this codebase (S-8, open/click tracking, is not built yet). The query
    # this used to run always returned 0, which the card rendered as
    # "confirmed zero engagement" to a setter about to make a call, rather
    # than "not tracked". "n/a" is honest — restore the real per-contact
    # query here once S-8 lands.
    return {
        "touches": touch_row or 0,
        "opens":   "n/a",
        "clicks":  "n/a",
    }

# ── Weak signal extraction ────────────────────────────────────────────────────

def _weakest_signals(signal_detail: Any, n: int = 3) -> list[str]:
    if not isinstance(signal_detail, dict):
        return []
    scored = []
    for key, entry in signal_detail.items():
        if not isinstance(entry, dict):
            continue
        max_val   = entry.get("points_possible", 0)
        score_val = entry.get("points_awarded", 0)
        if max_val > 0:
            scored.append((score_val / max_val, key))
    scored.sort(key=lambda x: x[0])
    return [_SIGNAL_LABELS.get(key, key.replace("_", " ").title()) for _, key in scored[:n]]

# ── Opener templates ──────────────────────────────────────────────────────────

def _suggest_opener(intent: Intent, contact_name: str, weak_signals: list[str]) -> str:
    first_name = (contact_name or "").split()[0] or "there"
    weakness   = weak_signals[0].lower() if weak_signals else "your online visibility"
    if intent == Intent.HOT_LEAD:
        return (
            f"Hi {first_name} — great timing. I noticed {weakness} is an area where there's "
            f"room to improve, which typically means owners are leaving money on the table. "
            f"I can show you what that looks like in 15 minutes — when works?"
        )
    if intent == Intent.WHALE_OWNER:
        weakness2 = weak_signals[1].lower() if len(weak_signals) > 1 else None
        gaps = f"{weakness} and {weakness2}" if weakness2 else weakness
        return (
            f"Hi {first_name} — for a portfolio of your size, the details compound fast. "
            f"I noticed {gaps} — at scale those gaps are some of the bigger revenue drains. "
            f"I'd love to walk you through our dedicated owner dashboard. When are you free?"
        )
    if intent == Intent.OBJECTION:
        return (
            f"Hi {first_name} — I completely understand. "
            f"Would it help if I shared how other owners in your county have handled this?"
        )
    return ""


def _format_thread(messages: list[dict]) -> str:
    if not messages:
        return "No prior messages on record."
    lines = []
    for msg in reversed(messages):
        ts = msg.get("received_at")
        ts_str = ts.strftime("%b %d") if isinstance(ts, datetime) else "–"
        body = (msg.get("body_text") or "").replace("\n", " ").strip()
        if len(body) > 140:
            body = body[:139] + "…"
        lines.append(f"[{ts_str}] _{body}_")
    return "\n".join(lines)

# ── Block builder ─────────────────────────────────────────────────────────────

def _build_blocks(
    *,
    db_id: int,
    client_id: str,
    intent: Intent,
    objection_subtype: Optional[str],
    sla_due_at: Optional[datetime],
    contact: Optional[dict],
    ovs: Optional[dict],
    prior_messages: list[dict],
    engagement: dict,
    body_text: str,
    hash_value: str,
) -> list[dict]:
    emoji        = _INTENT_EMOJI.get(intent, "📬")
    intent_label = intent.value.replace("_", " ").title()
    contact_name = ""
    if contact:
        contact_name = f"{contact.get('first_name') or ''} {contact.get('last_name') or ''}".strip()
    display_name = contact_name or "Unknown owner"
    header_text  = f"{emoji} {intent_label} — {display_name}"

    # SLA display
    sla_text = "Claim by *" + sla_due_at.strftime("%H:%M UTC") + "*" if sla_due_at else "No SLA set"

    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": header_text[:150], "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f":clock2: {sla_text}"}},
        {"type": "divider"},
    ]

    # Owner profile fields
    company   = (contact or {}).get("company_name") or "Unknown"
    doors     = (contact or {}).get("door_count_est")
    county    = (ovs or {}).get("county_slug") or (contact or {}).get("county_slug") or ""
    ovs_score = (ovs or {}).get("score_total")
    rank      = (ovs or {}).get("county_rank")

    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Company*\n{company}"},
        {"type": "mrkdwn", "text": f"*Doors*\n{doors if doors is not None else 'Unknown'}"},
        {"type": "mrkdwn", "text": f"*OVS Score*\n{ovs_score if ovs_score is not None else 'N/A'} / 100"},
        {"type": "mrkdwn", "text": f"*County Rank*\n{'#' + str(rank) + ' in ' + county if rank else 'N/A'}"},
    ]})

    weak_signals = _weakest_signals((ovs or {}).get("signal_detail")) if ovs else []
    if weak_signals:
        weak_lines = "\n".join(f"  {i+1}. {s}" for i, s in enumerate(weak_signals))
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*3 Weakest Areas*\n{weak_lines}"}})

    blocks.append({"type": "divider"})

    # Their message
    snippet = (body_text or "")[:500].replace("\n", "\n> ")
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Their message*\n> {snippet}"}})

    # Prior thread
    if prior_messages:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "*Prior thread*\n" + _format_thread(prior_messages)}})

    blocks.append({"type": "divider"})

    # Engagement
    blocks.append({"type": "section", "fields": [
        {"type": "mrkdwn", "text": f"*Touches sent*\n{engagement['touches']}"},
        {"type": "mrkdwn", "text": f"*Opens*\n{engagement['opens']}"},
        {"type": "mrkdwn", "text": f"*Clicks*\n{engagement['clicks']}"},
    ]})

    # Objection playbook
    if intent == Intent.OBJECTION:
        subtype  = objection_subtype or "other"
        playbook = OBJECTION_PLAYBOOKS.get(subtype, OBJECTION_PLAYBOOKS["other"])
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Playbook*\n{playbook}"}})

    # Suggested opener
    opener = _suggest_opener(intent, display_name, weak_signals)
    if opener:
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*\U0001f4ac Suggested opener*\n_{opener}_"}})

    blocks.append({"type": "divider"})

    # Claim button
    btn_value = json.dumps({"db_id": db_id, "client_id": client_id, "card_hash": hash_value}, separators=(",", ":"))
    blocks.append({"type": "actions", "elements": [{
        "type": "button",
        "style": "primary",
        "text": {"type": "plain_text", "text": "Claim this lead", "emoji": True},
        "action_id": "claim_context_card",
        "value": btn_value,
    }]})

    return blocks

# ── Public async entry point ──────────────────────────────────────────────────

async def post_context_card(
    *,
    db_id: int,
    client_id: str,
    sender_email: str,
    received_at: datetime,
    intent: Intent,
    result: Any,  # ClassificationResult — avoid circular import typing
    sla_due_at: Optional[datetime],
) -> Optional[dict]:
    """Build and post the context card to #blackink-setter.

    Opens its own system DB session for card data queries — the caller's
    transaction has already committed. Returns
    {"card_ts", "card_channel_id", "card_posted_at_iso"} on success, None
    if Slack is unconfigured or an error occurs.

    The caller (_write_card_meta in worker.py) persists the returned dict
    back to the inbound_messages row.
    """
    try:
        with get_system_db_context() as db:
            contact    = _fetch_contact(db, sender_email)
            company_id = (contact or {}).get("company_id")
            ovs        = _fetch_ovs(db, company_id)
            prior_msgs = _fetch_prior_messages(db, client_id, sender_email, db_id)
            engagement = _fetch_engagement(db, (contact or {}).get("contact_id"))
            body_row   = db.execute(
                text("SELECT body_text, sender_name FROM inbound_messages WHERE id = :id"),
                {"id": db_id},
            ).first()

        body_text   = body_row[0] if body_row else ""
        weak_signals = _weakest_signals((ovs or {}).get("signal_detail")) if ovs else []

        now           = datetime.now(timezone.utc)
        posted_at_iso = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        hash_value    = compute_card_hash(db_id, client_id, intent.value, posted_at_iso)

        objection_subtype = getattr(result, "objection_subtype", None)

        blocks = _build_blocks(
            db_id=db_id,
            client_id=client_id,
            intent=intent,
            objection_subtype=objection_subtype,
            sla_due_at=sla_due_at,
            contact=contact,
            ovs=ovs,
            prior_messages=prior_msgs,
            engagement=engagement,
            body_text=body_text or "",
            hash_value=hash_value,
        )

        emoji        = _INTENT_EMOJI.get(intent, "📬")
        fallback_txt = f"{emoji} {intent.value} from {sender_email} — claim in #blackink-setter"

        slack_result = await slack_post.post_action_card(
            channel_key="setter",
            text=fallback_txt,
            blocks=blocks,
        )

        log_event(
            client_id,
            "context_card_generated",
            entity_type="inbound_message",
            entity_id=str(db_id),
            payload={
                "intent_class":      intent.value,
                "sla_due_at":        sla_due_at.isoformat() if sla_due_at else None,
                "sender_email":      sender_email,
                "card_posted":       slack_result is not None,
                "objection_subtype": objection_subtype,
            },
            actor="respond_worker",
        )

        if not slack_result:
            return None

        return {
            "card_ts":           slack_result["message_ts"],
            "card_channel_id":   slack_result["channel_id"],
            "card_posted_at_iso": posted_at_iso,
        }

    except Exception:
        logger.exception(
            "context_cards.post_context_card: failed for db_id=%s client=%s", db_id, client_id
        )
        return None
