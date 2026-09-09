"""Sequencer core — runs post-approval: compliance re-check, mailbox pick,
at-most-once claim, send, and SENDING→SENT/FAILED resolution.

dispatch_touch is invoked by the DISPATCH_EMAIL_TOUCH work-order dispatcher
AFTER a human has approved the touch's Slack card. It therefore SENDS — it
does not queue another approval. The at-most-once guard is the DB
UNIQUE(run_id, touch_step) claim (ticket 22); a capped mailbox is a DEFER,
not a failure (ticket 08).
"""

import logging
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.compliance_gate import evaluate_touch_gate
from src.services.email_sender import EmailSender, build_email_sender
from src.services.email_unsubscribe import append_unsubscribe_footer, unsubscribe_url
from src.services.mailbox_dispatcher import (
    AllMailboxesCapped,
    NoMailboxAvailable,
    get_active_mailbox_for_client,
)
from src.services.sequence_dispatcher import (
    claim_touch,
    complete_run_with_cooling,
    get_touch_message_id,
    mark_failed,
    mark_sent,
)
from src.services.events import log_touch_dispatched

logger = logging.getLogger(__name__)

# Highest touch step in the sequence — dispatching it completes the run and
# opens the 30-day cooling window (wayfinder ticket 07).
_FINAL_TOUCH_STEP = 5

# Touch N threads to the reply chain of touch M (In-Reply-To + References).
# Touch 3 replies to Touch 1; Touch 5 replies to Touch 3.
_REPLY_TO_STEP: dict[int, int] = {3: 1, 5: 3}


@dataclass(frozen=True)
class TouchResult:
    contact_id: int
    touch_step: int
    # SENT | COMPLIANCE_BLOCK | NO_MAILBOX | VOLUME_CAP | ALREADY_CLAIMED
    # | SEND_FAILED | RECLAIMED | NO_CONTENT
    outcome: str
    message_id: str = ""


def _reassert_tenant(session: Session, client_id: str) -> None:
    """Re-issue SET LOCAL app.current_client_id after a commit. SET LOCAL is
    transaction-scoped, so once we commit the claim (finding #1 durability) the
    RLS binding is gone and the next statement (mark_sent/mark_failed) would run
    unscoped. Re-assert it on the fresh transaction."""
    if client_id:
        session.execute(text("SET LOCAL app.current_client_id = :cid"), {"cid": client_id})


def dispatch_touch(
    session: Session,
    contact,
    client_id: str,
    touch_step: int,
    run_id: str = "",
    sender: EmailSender | None = None,
    *,
    subject: str | None = None,
    body: str | None = None,
    template_version: str = "",
) -> TouchResult:
    """Run compliance, pick an under-cap mailbox, claim the slot, send, resolve.

    Content is fail-closed (finding #5): approved subject+body must be supplied
    by the caller (from the work-order payload). If either is missing we return
    NO_CONTENT and never send — placeholder/internal copy can no longer reach a
    real contact. Real copy is LLM-generated upstream (Dev 2, ticket 19).

    Durability (finding #1): the SENDING claim is committed in its own
    transaction BEFORE the external send, then SENT/FAILED is committed in a
    separate transaction. A crash after the send commits nothing new, leaving a
    durable SENDING row for the stuck-dispatch sweep to reconcile — the UNIQUE
    (run_id, touch_step) claim then blocks any re-send.
    """
    sender = sender or build_email_sender()

    if not subject or not body:
        logger.error(
            "sequence_orchestrator: NO_CONTENT contact_id=%s touch=%d — approved subject/body absent; not sending",
            contact.contact_id, touch_step,
        )
        return TouchResult(contact_id=contact.contact_id, touch_step=touch_step, outcome="NO_CONTENT")

    gate = evaluate_touch_gate(session, contact, client_id)
    if not gate.ready:
        logger.info(
            "sequence_orchestrator: compliance block contact_id=%s touch=%d reasons=%s",
            contact.contact_id, touch_step, gate.blocked_reasons,
        )
        return TouchResult(contact_id=contact.contact_id, touch_step=touch_step, outcome="COMPLIANCE_BLOCK")

    try:
        mailbox = get_active_mailbox_for_client(session, client_id)
    except AllMailboxesCapped as exc:
        # Not a failure — the caller defers the touch (pushes due_at forward).
        logger.info("sequence_orchestrator: all mailboxes capped for client_id=%s — deferring: %s", client_id, exc)
        return TouchResult(contact_id=contact.contact_id, touch_step=touch_step, outcome="VOLUME_CAP")
    except NoMailboxAvailable as exc:
        logger.error("sequence_orchestrator: no mailbox for client_id=%s — %s", client_id, exc)
        return TouchResult(contact_id=contact.contact_id, touch_step=touch_step, outcome="NO_MAILBOX")

    # Look up the prior touch's Message-ID so email clients thread the reply.
    # Done BEFORE the claim commit so it shares the RLS-scoped read transaction.
    in_reply_to: str | None = None
    if run_id and touch_step in _REPLY_TO_STEP:
        in_reply_to = get_touch_message_id(session, run_id, _REPLY_TO_STEP[touch_step])
        if in_reply_to:
            logger.debug(
                "sequence_orchestrator: threading touch=%d in_reply_to=%s", touch_step, in_reply_to
            )

    dispatch_id = claim_touch(session, client_id, run_id, touch_step, mailbox.mailbox_id)
    if dispatch_id is None:
        return TouchResult(contact_id=contact.contact_id, touch_step=touch_step, outcome="ALREADY_CLAIMED")

    # Durability boundary (finding #1): commit the SENDING claim in its OWN
    # transaction before the external send. If the worker dies after SMTP
    # accepts but before we record SENT, this row survives as SENDING (not
    # rolled back), the stuck-dispatch sweep alerts, and the UNIQUE claim blocks
    # any re-send. Re-assert the tenant binding for the post-commit transaction.
    session.commit()
    _reassert_tenant(session, client_id)

    # CLAUDE.md's mandatory one-click unsubscribe invariant — computed at
    # send time (not enrollment time) so it always reflects the address
    # actually being mailed to.
    unsub_url = unsubscribe_url(client_id, contact.email)
    body_with_footer = append_unsubscribe_footer(body, unsub_url)

    # send → UPDATE SENT/FAILED, each in the fresh (post-claim-commit) txn.
    try:
        result = sender.send(
            from_address=mailbox.mailbox_address,
            to_address=contact.email,
            subject=subject,
            body=body_with_footer,
            sending_domain=mailbox.sending_domain,
            in_reply_to=in_reply_to,
            list_unsubscribe_url=unsub_url,
        )
    except Exception as exc:  # noqa: BLE001 — any send failure resolves the row
        mark_failed(session, client_id, dispatch_id, str(exc))
        logger.error(
            "sequence_orchestrator: send FAILED contact_id=%s touch=%d dispatch=%s: %s",
            contact.contact_id, touch_step, dispatch_id, exc,
        )
        return TouchResult(contact_id=contact.contact_id, touch_step=touch_step, outcome="SEND_FAILED")

    if not mark_sent(session, client_id, dispatch_id, result.message_id):
        # Row was reclaimed mid-flight — the send happened but we can't record
        # SENT. Alerted via the stuck-SENDING sweep; do not double-send.
        return TouchResult(contact_id=contact.contact_id, touch_step=touch_step, outcome="RECLAIMED")

    log_touch_dispatched(
        session=session,
        client_id=client_id,
        contact_id=contact.contact_id,
        touch_step=touch_step,
        dispatch_id=dispatch_id,
        mailbox_id=mailbox.mailbox_id,
        sending_domain=mailbox.sending_domain,
        template_version=template_version,
        recipient_email=contact.email,
    )

    if touch_step >= _FINAL_TOUCH_STEP and run_id:
        complete_run_with_cooling(session, client_id, run_id)

    return TouchResult(
        contact_id=contact.contact_id,
        touch_step=touch_step,
        outcome="SENT",
        message_id=result.message_id,
    )
