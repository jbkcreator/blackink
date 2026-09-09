"""Relay worker — dispatches approved campaign sends via Instantly or SMTP.

Reads from relay:sends (published by node_relay_dispatch when a Slack approval
clears wait_approve). For each message:

  1. Check CLIENT halt — leave in PEL if active (claim_stale picks up later).
  2. Fetch the campaign draft from Redis (stored by cora/worker.py).
  3. Fetch contact + company info from DB (RLS-scoped to client_id).
  4. Pick the least-recently-used warmed mailbox via mailbox_dispatcher.
  5a. If INSTANTLY_ENABLED: create Instantly campaign + add lead + activate.
  5b. Elif EMAIL_SENDING_ENABLED: send Touch 1 via direct SMTP.
  5c. Else: log touch_send_blocked event and ack (don't leave in PEL forever).
  6. Log touch_sent (or touch_send_blocked) event to events table.
  7. Ack message.

Usage
─────
    python -m src.agents.relay.worker

Environment
───────────
    DATABASE_URL, REDIS_URL — required.
    INSTANTLY_ENABLED=True + INSTANTLY_API_KEY — to use Instantly dispatch.
    EMAIL_SENDING_ENABLED=True — to use direct SMTP (fallback to Instantly).
"""
from __future__ import annotations

import logging
import os
import signal
import socket
import time
from typing import Optional

from src.agents.relay import halt_service
from src.agents.relay import sends_queue as sq
from src.agents.relay.sync import sync_halts_from_db

logger = logging.getLogger(__name__)

CONSUMER_NAME      = f"relay-worker-{socket.gethostname()}-{os.getpid()}"
POLL_INTERVAL_MS   = 1_000
CLAIM_INTERVAL_SEC = 60
CLAIM_MIN_IDLE_MS  = 60_000


class RelayWorker:
    def __init__(self) -> None:
        self._running = True
        self._last_claim = 0.0
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT,  self._stop)

    def _stop(self, *_) -> None:
        logger.info("relay.worker: shutdown requested — draining in-flight work")
        self._running = False

    def run_forever(self) -> None:
        sq.ensure_group()
        sync_halts_from_db()
        logger.info("relay.worker: starting consumer=%s", CONSUMER_NAME)

        while self._running:
            now = time.monotonic()
            if now - self._last_claim >= CLAIM_INTERVAL_SEC:
                for msg in sq.claim_stale(CONSUMER_NAME, min_idle_ms=CLAIM_MIN_IDLE_MS):
                    logger.info("relay.worker: reclaimed stale message_id=%s", msg.message_id)
                    self._process(msg)
                self._last_claim = now

            for msg in sq.read_batch(CONSUMER_NAME, count=1, block_ms=POLL_INTERVAL_MS):
                if not self._running:
                    break
                self._process(msg)

        logger.info("relay.worker: stopped consumer=%s", CONSUMER_NAME)

    def _process(self, msg: sq.SendMessage) -> None:
        if halt_service.is_halted(client_id=msg.client_id):
            logger.info(
                "relay.worker: CLIENT halt active — leaving in PEL "
                "work_order_id=%s client_id=%s",
                msg.work_order_id, msg.client_id,
            )
            return  # leave in PEL; claim_stale will retry when halt lifts

        try:
            _dispatch(msg)
            sq.ack(msg.message_id)
        except Exception:
            logger.exception(
                "relay.worker: dispatch failed — leaving in PEL message_id=%s "
                "work_order_id=%s",
                msg.message_id, msg.work_order_id,
            )


def _block_send(msg: sq.SendMessage, reason: str) -> None:
    from src.services.events import log_event
    logger.warning(
        "relay.worker: send blocked work_order_id=%s reason=%s company_id=%s",
        msg.work_order_id, reason, msg.company_id,
    )
    log_event(
        client_id=msg.client_id,
        event_type="touch_send_blocked",
        entity_type="work_order",
        entity_id=msg.work_order_id,
        payload={"reason": reason, "company_id": msg.company_id},
    )


def _dispatch(msg: sq.SendMessage) -> None:
    """Resolve draft → contact → mailbox → send."""
    from sqlalchemy import text

    from src.agents.cora import draft_store
    from src.agents.relay.sends_queue import SendMessage
    from src.core.database import get_db_context, get_system_db_context
    from src.services.events import log_event
    from src.services.outbound_templates import build_instantly_sequence, resolve_tags
    from config.settings import get_settings

    settings = get_settings()

    # ── Fetch draft ──────────────────────────────────────────────────────────
    steps = draft_store.get(msg.work_order_id)
    if not steps:
        logger.error(
            "relay.worker: draft expired or missing work_order_id=%s — dead-lettering",
            msg.work_order_id,
        )
        log_event(
            client_id=msg.client_id,
            event_type="touch_send_blocked",
            entity_type="work_order",
            entity_id=msg.work_order_id,
            payload={"reason": "DRAFT_EXPIRED", "work_order_id": msg.work_order_id},
        )
        return  # will be acked by caller — not re-retriable

    # ── Fetch contact + company ──────────────────────────────────────────────
    contact_email = None
    from src.agents.ink.subagents.pdf_generator.pdf_report import _fmt_latency
    audit_speed  = _fmt_latency(msg.latency_sec) if msg.latency_sec is not None else ""
    loss_dollars = f"${msg.loss_est:,}" if msg.loss_est else ""

    context: dict = {
        "company":      msg.company_id,
        "client_firm":  msg.client_id,
        "first_name":   "there",
        "city":         "",
        "door_count":   "",
        "audit_speed":  audit_speed,
        "loss_dollars": loss_dollars,
        "video_url":    msg.landing_url or "",
    }

    try:
        with get_db_context(client_id=msg.client_id) as db:
            co_row = db.execute(
                text(
                    "SELECT c.company_name, co.county_name, c.door_count_est, "
                    "       ct.email, ct.first_name, "
                    "       ct.is_opted_out, ct.suppression_state, ct.outbound_paused_at "
                    "FROM   companies c "
                    "LEFT JOIN counties co ON co.county_slug = c.county_slug "
                    "LEFT JOIN contacts ct ON ct.company_id = c.company_id "
                    "   AND ct.contact_role_type IN ('DECISION_MAKER','OWNER_BROKER_MD') "
                    "WHERE  c.company_id = :cid "
                    "LIMIT 1"
                ),
                {"cid": msg.company_id},
            ).fetchone()
        if co_row:
            contact_email         = co_row.email
            context["company"]    = co_row.company_name  or msg.company_id
            context["city"]       = co_row.county_name   or ""
            context["door_count"] = str(co_row.door_count_est or "")
            context["first_name"] = co_row.first_name    or "there"

            # Re-check compliance state at dispatch time — contact can opt out,
            # be suppressed, or be paused between campaign creation and approval.
            if co_row.is_opted_out:
                _block_send(msg, "CONTACT_OPT_OUT"); return
            if co_row.suppression_state:
                _block_send(msg, "CONTACT_SUPPRESSED"); return
            if co_row.outbound_paused_at is not None:
                _block_send(msg, "CONTACT_PAUSED"); return
    except Exception as exc:
        logger.warning(
            "relay.worker: DB lookup failed company_id=%s: %s — continuing",
            msg.company_id, exc,
        )

    try:
        with get_system_db_context() as db:
            cl_row = db.execute(
                text("SELECT display_name FROM clients WHERE client_id = :cid"),
                {"cid": msg.client_id},
            ).fetchone()
        if cl_row:
            context["client_firm"] = cl_row.display_name
    except Exception as exc:
        logger.warning("relay.worker: client lookup failed client_id=%s: %s", msg.client_id, exc)

    if not contact_email:
        logger.error(
            "relay.worker: no contact email found company_id=%s — blocking send",
            msg.company_id,
        )
        log_event(
            client_id=msg.client_id,
            event_type="touch_send_blocked",
            entity_type="work_order",
            entity_id=msg.work_order_id,
            payload={"reason": "NO_CONTACT_EMAIL", "company_id": msg.company_id},
        )
        return

    # ── Dispatch via Instantly ───────────────────────────────────────────────
    if settings.instantly_enabled:
        _dispatch_via_instantly(msg, steps, context, contact_email, settings)
        log_event(
            client_id=msg.client_id,
            event_type="touch_sent",
            entity_type="work_order",
            entity_id=msg.work_order_id,
            payload={
                "channel":        "instantly",
                "company_id":     msg.company_id,
                "campaign_id":    msg.campaign_id,
                "work_order_id":  msg.work_order_id,
                "contact_email":  contact_email,
            },
        )
        return

    # ── Dispatch via direct SMTP ─────────────────────────────────────────────
    if settings.email_sending_enabled:
        _dispatch_via_smtp(msg, steps, context, contact_email, settings)
        log_event(
            client_id=msg.client_id,
            event_type="touch_sent",
            entity_type="work_order",
            entity_id=msg.work_order_id,
            payload={
                "channel":       "smtp",
                "company_id":    msg.company_id,
                "work_order_id": msg.work_order_id,
                "contact_email": contact_email,
            },
        )
        return

    # ── Neither enabled ──────────────────────────────────────────────────────
    logger.warning(
        "relay.worker: email sending disabled — blocking send work_order_id=%s",
        msg.work_order_id,
    )
    log_event(
        client_id=msg.client_id,
        event_type="touch_send_blocked",
        entity_type="work_order",
        entity_id=msg.work_order_id,
        payload={"reason": "EMAIL_SENDING_DISABLED", "work_order_id": msg.work_order_id},
    )


def _dispatch_via_instantly(
    msg:           sq.SendMessage,
    steps:         list,
    context:       dict,
    contact_email: str,
    settings,
) -> None:
    """Create an Instantly campaign, add the lead, and activate it."""
    from src.services.instantly_service import InstantlyService, InstantlyDisabledError
    from src.services.outbound_templates import build_instantly_sequence

    svc = InstantlyService()

    sequence_steps = build_instantly_sequence(steps, context)
    campaign_name  = f"Blackink - {context['company']} - {msg.work_order_id[:8]}"

    campaign_id = svc.create_campaign(name=campaign_name, sequence_steps=sequence_steps)
    if not campaign_id:
        raise RuntimeError(f"Instantly create_campaign failed for work_order_id={msg.work_order_id}")

    lead = {
        "email":        contact_email,
        "first_name":   context.get("first_name", ""),
        "company_name": context.get("company", ""),
        # Custom variables passed through for Instantly merge at send time.
        "audit_speed":  context.get("audit_speed", ""),
        "loss_dollars": context.get("loss_dollars", ""),
        "video_url":    context.get("video_url", ""),
    }
    if not svc.add_leads(campaign_id, [lead]):
        raise RuntimeError(f"Instantly add_leads failed campaign_id={campaign_id}")

    if not svc.activate_campaign(campaign_id):
        raise RuntimeError(f"Instantly activate_campaign failed campaign_id={campaign_id}")

    logger.info(
        "relay.worker: Instantly campaign activated work_order_id=%s campaign_id=%s",
        msg.work_order_id, campaign_id,
    )


def _read_file_url(url: str) -> Optional[bytes]:
    """Read bytes from a file:// URL. Returns None on any error."""
    if not url or not url.startswith("file://"):
        return None
    try:
        import urllib.request
        with urllib.request.urlopen(url) as f:
            return f.read()
    except Exception as exc:
        logger.warning("relay.worker: could not read file URL %s: %s", url, exc)
        return None


def _dispatch_via_smtp(
    msg:           sq.SendMessage,
    steps:         list,
    context:       dict,
    contact_email: str,
    settings,
) -> None:
    """Send Touch 1 directly via SMTP. Subsequent touches require a sweep."""
    import base64

    from src.services.email_dispatch import SmtpEmailProvider
    from src.services.mailbox_dispatcher import get_active_mailbox_for_client, NoMailboxAvailable
    from src.services.outbound_templates import resolve_tags
    from src.core.database import get_db_context
    from src.core.token_crypto import decrypt_token

    touch1    = steps[0]
    subject   = resolve_tags(touch1["subject"], context)
    body_text = resolve_tags(touch1["body"], context).replace("\n", "<br>")

    # Embed GIF thumbnail at top of email if available
    gif_block = ""
    gif_bytes = _read_file_url(msg.gif_url) if msg.gif_url else None
    if gif_bytes:
        b64 = base64.b64encode(gif_bytes).decode()
        img_tag = f'<img src="data:image/gif;base64,{b64}" width="600" style="max-width:100%;display:block;" alt="Audit snapshot"/>'
        # Wrap in link if a landing URL exists (opens video/landing page on click)
        if msg.landing_url:
            gif_block = f'<p><a href="{msg.landing_url}" target="_blank">{img_tag}</a></p>'
        else:
            gif_block = f"<p>{img_tag}</p>"
        logger.info("relay.worker: GIF embedded (%d bytes) work_order_id=%s", len(gif_bytes), msg.work_order_id)

    body_html = gif_block + body_text

    # Build PDF attachment list — audit PDF + fee-stack one-pager
    pdf_bytes       = _read_file_url(msg.pdf_url)       if msg.pdf_url       else None
    fee_stack_bytes = _read_file_url(msg.fee_stack_url) if msg.fee_stack_url else None
    if pdf_bytes:
        logger.info("relay.worker: audit PDF attached (%d bytes) work_order_id=%s", len(pdf_bytes), msg.work_order_id)
    if fee_stack_bytes:
        logger.info("relay.worker: fee-stack PDF attached (%d bytes) work_order_id=%s", len(fee_stack_bytes), msg.work_order_id)

    try:
        with get_db_context(client_id=msg.client_id) as db:
            mailbox = get_active_mailbox_for_client(db, msg.client_id)
            smtp_row = db.execute(
                __import__("sqlalchemy").text(
                    "SELECT smtp_host, smtp_port, smtp_username, smtp_password_encrypted "
                    "FROM   mailboxes "
                    "WHERE  id = :mid"
                ),
                {"mid": mailbox.mailbox_id},
            ).fetchone()

        if not smtp_row or not smtp_row.smtp_host:
            raise RuntimeError(f"No SMTP credentials for mailbox_id={mailbox.mailbox_id}")

        provider = SmtpEmailProvider(
            host=smtp_row.smtp_host,
            port=smtp_row.smtp_port or 587,
            username=smtp_row.smtp_username,
            password=decrypt_token(smtp_row.smtp_password_encrypted),
            from_address=mailbox.mailbox_address,
        )

        attachments = []
        if pdf_bytes:
            attachments.append((pdf_bytes, "audit-report.pdf", "pdf"))
        if fee_stack_bytes:
            attachments.append((fee_stack_bytes, "fee-stack-opportunity.pdf", "pdf"))

        if attachments:
            provider.send_with_attachments(
                to=contact_email,
                reply_to=mailbox.mailbox_address,
                bcc=mailbox.mailbox_address,
                subject=subject,
                html_body=body_html,
                attachments=attachments,
            )
        else:
            provider.send_plain(
                to=contact_email,
                reply_to=mailbox.mailbox_address,
                bcc=mailbox.mailbox_address,
                subject=subject,
                html_body=body_html,
            )

        logger.info(
            "relay.worker: SMTP Touch 1 sent work_order_id=%s mailbox=%s to=%s "
            "gif=%s pdf=%s fee_stack=%s",
            msg.work_order_id, mailbox.mailbox_address, contact_email,
            bool(gif_bytes), bool(pdf_bytes), bool(fee_stack_bytes),
        )
    except NoMailboxAvailable as exc:
        raise RuntimeError(f"No warmed mailbox available: {exc}") from exc


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    worker = RelayWorker()
    worker.run_forever()


if __name__ == "__main__":
    main()
