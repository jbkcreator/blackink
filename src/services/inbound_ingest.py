"""Inbound reply ingestion (Task 3.1.3 / ticket 30).

Owns the cross-client work for a forwarded prospect reply: client resolution,
BCC-loop break, dedup, two-tier attribution, persistence, and the
#sales-replies Slack card. This is the service layer that holds the
`blackink_system` BYPASSRLS session — the webhook router (src/api/) must never
open that session itself (CLAUDE.md invariant), so it hands the parsed payload
here and gets back a small result dict for its HTTP response.

All DB access uses sqlalchemy.text() with named binds.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import text

from src.core.database import get_system_db_context
from src.services.events import already_logged_for_dispatch, log_event
from src.services.inbound_attribution import (
    attribute,
    is_bcc_echo,
    resolve_client_from_alias,
    strip_display_name,
)
from src.services.ovs_lookup import fetch_latest_ovs, ovs_card_lines
from src.services.slack import post as slack_post
from src.services.slack.listeners import sales_reply_content_blocks
from src.services.stl_cadence import stop_active_stl_cadences

logger = logging.getLogger(__name__)


def _idempotency_key(destination_address: str, original_message_id: str) -> str:
    """Same derivation as src/api/inbound_router.py's own helper — kept as a
    separate copy rather than a shared import because that module's version
    is private (leading underscore, not meant as a public API) and this repo
    has no third shared home for it yet; both must stay byte-for-byte
    identical since they key the SAME UNIQUE(idempotency_key) constraint."""
    raw = f"{destination_address}:{original_message_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


@dataclass
class InboundParsed:
    """The verified, parsed fields of one Mailgun inbound POST."""

    to_alias: str
    from_raw: str
    subject: Optional[str]
    raw_body: Optional[str]
    inbound_message_id: str
    in_reply_to: Optional[str]


async def ingest_inbound_reply(parsed: InboundParsed) -> dict:
    """Resolve, persist, and surface one inbound reply.

    Returns a small dict describing the outcome (the router uses it verbatim as
    the 200 body): a discard reason for messages we drop on purpose, or
    ``{"status": "ok", "inbound_id": ...}`` once stored and posted.
    """
    from_address = strip_display_name(parsed.from_raw) or parsed.from_raw
    received_at = datetime.now(timezone.utc)

    logger.info(
        "[inbound_ingest] received from=%s alias=%s in_reply_to=%s",
        from_address,
        parsed.to_alias,
        parsed.in_reply_to and parsed.in_reply_to[:30],
    )

    with get_system_db_context() as session:
        client_id = resolve_client_from_alias(session, parsed.to_alias)
        if client_id is None:
            logger.warning("[inbound_ingest] unknown alias=%s — discarding", parsed.to_alias)
            return {"status": "discarded", "reason": "unknown_alias"}

        # Task 4.2.2 — a Speed-to-Lead lead who replies must stop their inbound
        # cadence. STL leads are contactless (contact_id NULL), so the cold
        # attribution below can never latch them; this is a separate, additive
        # stop keyed purely on the sender email against ARMED STL cadences.
        #
        # Committed up front, in its own transaction, on purpose: the cold-reply
        # persistence below (owned by Task 3.1.3) targets an older inbound_messages
        # shape and is independently broken on the current deployed schema — the
        # STL stop must not be coupled to it. Email-matched, so a BCC echo (from
        # our own mailbox, not the prospect) can never match a prospect's cadence.
        stop_active_stl_cadences(session, client_id, from_address, "REPLY")
        session.commit()

        if is_bcc_echo(session, parsed.inbound_message_id):
            logger.info(
                "[inbound_ingest] BCC echo message_id=%s — dropping",
                parsed.inbound_message_id[:40],
            )
            return {"status": "discarded", "reason": "bcc_echo"}

        # Dedup key matches src/api/inbound_router.py's own derivation exactly
        # (see _idempotency_key's docstring) — this is the actual UNIQUE
        # constraint on inbound_messages (uq_inbound_messages_idempotency),
        # not a UNIQUE on original_message_id alone. Bug fix (found during
        # S-8/S-11 task-analysis): this function previously deduped and
        # inserted against column names (message_id/from_address/to_alias/
        # raw_body) and a manually-assigned UUID `id` that do not exist on
        # the real table (BIGSERIAL id; original_message_id/sender_email/
        # destination_address/body_text instead) — every call would have
        # raised UndefinedColumn against a real Postgres. Fixed to the real
        # schema; see docs/plans/2026-09-10-s8-s11-tracking-and-reply-send.md
        # "Shared blocking prerequisite".
        idem_key = _idempotency_key(parsed.to_alias, parsed.inbound_message_id)
        existing = session.execute(
            text("SELECT id FROM inbound_messages WHERE idempotency_key = :key LIMIT 1"),
            {"key": idem_key},
        ).first()
        if existing is not None:
            logger.info("[inbound_ingest] duplicate idempotency_key — already stored, no-op")
            return {"status": "discarded", "reason": "duplicate"}

        result = attribute(
            session,
            client_id=client_id,
            in_reply_to=parsed.in_reply_to,
            from_address=from_address,
        )

        inserted = session.execute(
            text(
                """
                INSERT INTO inbound_messages
                    (client_id, idempotency_key, destination_address, original_message_id,
                     contact_id, run_id, sender_email, in_reply_to, subject, body_text,
                     attribution_status, received_at)
                VALUES
                    (:client_id, :idempotency_key, :destination_address, :original_message_id,
                     :contact_id, :run_id, :sender_email, :in_reply_to, :subject, :body_text,
                     :attribution_status, :received_at)
                ON CONFLICT (idempotency_key) DO NOTHING
                RETURNING id
                """
            ),
            {
                "client_id": result.client_id,
                "idempotency_key": idem_key,
                "destination_address": parsed.to_alias,
                "original_message_id": parsed.inbound_message_id,
                "contact_id": result.contact_id,
                "run_id": result.run_id,
                "sender_email": from_address,
                "in_reply_to": parsed.in_reply_to,
                "subject": parsed.subject,
                "body_text": parsed.raw_body,
                "attribution_status": result.attribution_status,
                "received_at": received_at,
            },
        ).first()
        if inserted is None:
            # ON CONFLICT DO NOTHING fired — a concurrent duplicate raced the
            # dedup read above and won the insert. Not an error, just a no-op,
            # same outcome as the pre-existing `existing is not None` branch.
            logger.warning("[inbound_ingest] INSERT conflict on idempotency_key — concurrent duplicate")
            return {"status": "discarded", "reason": "duplicate_race"}
        # `id` is BIGSERIAL, not the UUID this code used to assume — keep the
        # real int for any query comparing against the bigint column
        # (_recent_thread's exclude_id), and a str only for JSON/Slack/log
        # contexts (a bigint compared against a text bind has no implicit
        # cast in Postgres and would raise).
        inbound_id_int = inserted.id
        inbound_id = str(inbound_id_int)

        # Single write path for `events` (src/services/events.py's own
        # invariant) — this call site used to INSERT raw SQL directly; folded
        # onto log_event() while fixing the column-mismatch bug above rather
        # than leaving one old-style and one new-style events write
        # side by side in the same function.
        log_event(
            result.client_id,
            "inbound_reply_received",
            entity_type="inbound_message",
            entity_id=inbound_id,
            payload={
                "inbound_id": inbound_id,
                "original_message_id": parsed.inbound_message_id,
                "sender_email": from_address,
                "attribution_status": result.attribution_status,
                "contact_id": result.contact_id,
                "run_id": result.run_id,
                "channel": "email",
            },
            actor="mailgun_inbound",
            session=session,
        )

        # S-8 — email_replied, ONLY for a Tier-1 (message-id/In-Reply-To)
        # attributed reply, which is the only case with a resolvable
        # dispatch_id to attribute the reply to. A Tier-2 (sender-email-only)
        # match still posts to #sales-replies as usual, above — it just
        # doesn't count toward the digest's reply-rate metric, since there is
        # no specific outbound send to credit it against. See
        # docs/plans/2026-09-10-s8-s11-tracking-and-reply-send.md's S-8
        # section for why this is a stated scope line, not a silent gap.
        if result.run_id and result.touch_step:
            dispatch_row = session.execute(
                text(
                    "SELECT dispatch_id FROM sequence_touch_dispatches "
                    "WHERE run_id = :run_id AND touch_step = :touch_step AND status = 'SENT'"
                ),
                {"run_id": result.run_id, "touch_step": result.touch_step},
            ).first()
            if dispatch_row is not None:
                dispatch_id = str(dispatch_row.dispatch_id)
                # Code-review fix: a prospect replying more than once to the
                # SAME touch (two separate inbound_messages rows, each its
                # own idempotency_key, both Tier-1 attributed to this
                # dispatch_id) would otherwise double-count toward
                # daily_digest.py's reply_rate_pct — the exact class of
                # inflation email_opened/email_clicked already guard
                # against. Shared helper + a real DB-level partial unique
                # index (migrations/apply_events_dispatch_dedup_index.py)
                # back this the same way for all three event types.
                if not already_logged_for_dispatch(session, result.client_id, "email_replied", dispatch_id):
                    log_event(
                        result.client_id,
                        "email_replied",
                        entity_type="contact",
                        entity_id=str(result.contact_id),
                        payload={"dispatch_id": dispatch_id},
                        actor="mailgun_inbound",
                        session=session,
                    )

        thread_lines = _recent_thread(session, contact_id=result.contact_id, run_id=result.run_id, exclude_id=inbound_id_int)
        ovs_lines = ovs_card_lines(fetch_latest_ovs(session, result.firm_company_id))

        session.commit()

    card_text = (
        f"💬 Reply from {from_address}"
        + (f" · {result.contact_name}" if result.contact_name else "")
        + (f" ({result.firm_name})" if result.firm_name else "")
        + f" [{result.attribution_status}]"
    )
    blocks = sales_reply_content_blocks(
        from_address=from_address,
        contact_name=result.contact_name,
        firm_name=result.firm_name,
        run_id=result.run_id,
        touch_step=result.touch_step,
        attribution_status=result.attribution_status,
        subject=parsed.subject,
        raw_body=parsed.raw_body,
        contact_id=result.contact_id,
        client_id=result.client_id,
        inbound_id=inbound_id,
        firm_domain=result.firm_domain,
        thread_lines=None,  # history now posted as real threaded replies (below), not crammed in-card
        door_count=result.door_count,
        ovs_lines=ovs_lines,
    )
    # Colored side bar: green attributed / amber unattributed — quick triage cue.
    bar = "#2eb67d" if result.attribution_status == "attributed" else "#e8912d"
    card_ts = await slack_post.post_notice(
        channel_key="replies",
        text=card_text,
        attachments=[{"color": bar, "blocks": blocks}],
    )

    # Threaded history: post the last-3 prior messages as real replies under the
    # card (v2 §3.1.3 "message thread history"), oldest → newest so the thread
    # reads chronologically. Rep's own Reply-in-Thread lands in the same thread.
    if card_ts and thread_lines:
        for line in reversed(thread_lines):
            await slack_post.post_notice(channel_key="replies", text=line, thread_ts=card_ts)

    logger.info(
        "[inbound_ingest] stored inbound_id=%s attribution=%s",
        inbound_id,
        result.attribution_status,
    )
    return {"status": "ok", "inbound_id": inbound_id}


def _recent_thread(session, *, contact_id, run_id, exclude_id: int) -> list[str]:
    """Last 3 messages for this contact's thread (v2 §3.1.3): prior inbound
    replies plus our own outbound touches, newest-first, rendered as card lines.
    Empty when the reply is unattributed (no contact to gather a thread for).
    exclude_id is the real BIGINT inbound_messages.id (not a string) — see
    ingest_inbound_reply's own comment on why that distinction matters here."""
    if not contact_id:
        return []

    events: list[tuple] = []  # (sort_key_datetime, rendered_line)

    inbound = session.execute(
        text(
            "SELECT received_at, subject, body_text FROM inbound_messages "
            "WHERE contact_id = :cid AND id <> :exclude "
            "ORDER BY received_at DESC LIMIT 3"
        ),
        {"cid": contact_id, "exclude": exclude_id},
    ).mappings().all()
    for r in inbound:
        snippet = (r["body_text"] or r["subject"] or "").strip().replace("\n", " ")[:80]
        events.append((r["received_at"], f"⬅️ _{r['received_at']:%b %d}_ — {snippet}"))

    if run_id:
        outbound = session.execute(
            text(
                "SELECT sent_at, touch_step FROM sequence_touch_dispatches "
                "WHERE run_id = :run AND status = 'SENT' AND sent_at IS NOT NULL "
                "ORDER BY sent_at DESC LIMIT 3"
            ),
            {"run": run_id},
        ).mappings().all()
        for r in outbound:
            events.append((r["sent_at"], f"➡️ _{r['sent_at']:%b %d}_ — Touch {r['touch_step']} sent"))

    events.sort(key=lambda e: e[0], reverse=True)
    return [line for _, line in events[:3]]
