"""Unit tests for S-11 — the real reply-send and Book Meeting handlers on
#sales-replies cards (previously: Reply in Thread only posted to Slack,
never emailed the prospect; Book Meeting didn't exist).

No live DB: get_db_context is patched to a fake session; approver_authorized
is patched True unless a test specifically checks the rejection path.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.slack.listeners import (
    handle_book_meeting,
    handle_reply_in_thread,
    handle_reply_thread_modal_submit,
)
from src.services.mailbox_dispatcher import AllMailboxesCapped, NoMailboxAvailable


def _run(coro):
    return asyncio.run(coro)


def _fake_ctx(session):
    @contextmanager
    def _ctx(client_id=None):
        yield session

    return _ctx


def _inbound_row(*, status="PENDING", contact_id=7, sender_email="prospect@example.com", subject="Hello"):
    return SimpleNamespace(
        id="inbound-1",
        client_id="acme_pm",
        contact_id=contact_id,
        sender_email=sender_email,
        original_message_id="<orig@example.com>",
        subject=subject,
        status=status,
    )


def _fake_session(*, inbound_row, opted_out=False):
    session = MagicMock()

    def _execute(stmt, params=None, *a, **kw):
        sql = str(stmt)
        result = MagicMock()
        if "FROM inbound_messages WHERE id" in sql:
            result.first.return_value = inbound_row
        elif "FROM contacts WHERE contact_id" in sql:
            result.first.return_value = SimpleNamespace(is_opted_out=opted_out)
        else:
            result.first.return_value = None
        return result

    session.execute.side_effect = _execute
    return session


def _mailbox():
    return SimpleNamespace(mailbox_id=1, mailbox_address="sales@acme.com", sending_domain="acme-out.com")


# ── handle_reply_in_thread — modal-open value plumbing ──────────────────────

def test_reply_in_thread_passes_card_value_into_private_metadata():
    """Regression for the bug: action['value'] (inbound_id/contact_id/
    client_id) used to be silently discarded — the modal only carried
    {channel, ts}."""
    client = AsyncMock()
    body = {
        "trigger_id": "trigger-1",
        "channel": {"id": "C123"},
        "container": {"message_ts": "1700000000.0001"},
    }
    action = {"value": json.dumps({"inbound_id": "inbound-1", "contact_id": 7, "client_id": "acme_pm"})}
    _run(handle_reply_in_thread(ack=AsyncMock(), body=body, respond=AsyncMock(), client=client, action=action))

    view = client.views_open.call_args.kwargs["view"]
    meta = json.loads(view["private_metadata"])
    assert meta == {"channel": "C123", "ts": "1700000000.0001", "inbound_id": "inbound-1", "contact_id": 7, "client_id": "acme_pm"}


# ── handle_reply_thread_modal_submit ─────────────────────────────────────

def _submit_body(text_val="Thanks for reaching out!", **meta_overrides):
    meta = {"channel": "C123", "ts": "1700000000.0001", "inbound_id": "inbound-1", "contact_id": 7, "client_id": "acme_pm"}
    meta.update(meta_overrides)
    body = {"user": {"id": "U1"}}
    view = {
        "private_metadata": json.dumps(meta),
        "state": {"values": {"reply_block": {"reply_text": {"value": text_val}}}},
    }
    return body, view


def test_reply_send_success_emails_prospect_marks_responded_and_posts_thread():
    row = _inbound_row(status="PENDING")
    session = _fake_session(inbound_row=row)
    client = AsyncMock()
    body, view = _submit_body()

    fake_sender = MagicMock()
    with patch("src.services.slack.listeners.approver_authorized", return_value=True), \
         patch("src.services.slack.listeners.get_db_context", _fake_ctx(session)), \
         patch("src.services.slack.listeners.get_active_mailbox_for_client", return_value=_mailbox()), \
         patch("src.services.slack.listeners.build_email_sender", return_value=fake_sender), \
         patch("src.services.slack.listeners.unsubscribe_url", return_value="https://x/unsub"), \
         patch("src.services.slack.listeners.append_unsubscribe_footer", side_effect=lambda b, u: b), \
         patch("src.services.slack.listeners._shared_log_event") as mock_log:
        _run(handle_reply_thread_modal_submit(ack=AsyncMock(), body=body, view=view, client=client))

    fake_sender.send.assert_called_once()
    send_kwargs = fake_sender.send.call_args.kwargs
    assert send_kwargs["to_address"] == "prospect@example.com"
    assert send_kwargs["in_reply_to"] == "<orig@example.com>"
    # Marked RESPONDED — mailbox_dispatcher's rolling-24h cap subquery counts this.
    responded_calls = [c for c in session.execute.call_args_list if "SET status = 'RESPONDED'" in str(c.args[0])]
    assert len(responded_calls) == 1
    assert mock_log.call_args.args[1] == "sales_reply_sent"
    client.chat_postMessage.assert_awaited_once()
    assert "sent" in client.chat_postMessage.await_args.kwargs["text"].lower()


def test_reply_send_rejects_unauthorized_user():
    """Code-review fix (Critical): this handler sends a real outbound email
    and must check approver_authorized() the same as opt_out_contact/
    book_meeting/every work-order decision — confirmed unauthorized users
    are blocked before any DB session even opens."""
    body, view = _submit_body()
    ack = AsyncMock()
    with patch("src.services.slack.listeners.approver_authorized", return_value=False), \
         patch("src.services.slack.listeners.get_db_context") as mock_ctx:
        _run(handle_reply_thread_modal_submit(ack=ack, body=body, view=view, client=AsyncMock()))
    mock_ctx.assert_not_called()
    assert ack.await_args.kwargs.get("response_action") == "errors"
    assert "not authorized" in str(ack.await_args.kwargs["errors"]).lower()


def test_reply_send_is_idempotent_on_already_responded():
    row = _inbound_row(status="RESPONDED")
    session = _fake_session(inbound_row=row)
    client = AsyncMock()
    body, view = _submit_body()
    ack = AsyncMock()

    fake_sender = MagicMock()
    with patch("src.services.slack.listeners.approver_authorized", return_value=True), \
         patch("src.services.slack.listeners.get_db_context", _fake_ctx(session)), \
         patch("src.services.slack.listeners.build_email_sender", return_value=fake_sender):
        _run(handle_reply_thread_modal_submit(ack=ack, body=body, view=view, client=client))

    fake_sender.send.assert_not_called()
    client.chat_postMessage.assert_not_awaited()
    ack.assert_awaited_once()
    assert ack.await_args.kwargs.get("response_action") == "errors"


def test_reply_send_blocked_for_opted_out_contact():
    row = _inbound_row(status="PENDING")
    session = _fake_session(inbound_row=row, opted_out=True)
    client = AsyncMock()
    body, view = _submit_body()
    ack = AsyncMock()

    fake_sender = MagicMock()
    with patch("src.services.slack.listeners.approver_authorized", return_value=True), \
         patch("src.services.slack.listeners.get_db_context", _fake_ctx(session)), \
         patch("src.services.slack.listeners.build_email_sender", return_value=fake_sender):
        _run(handle_reply_thread_modal_submit(ack=ack, body=body, view=view, client=client))

    fake_sender.send.assert_not_called()
    assert ack.await_args.kwargs.get("response_action") == "errors"
    assert "opted out" in str(ack.await_args.kwargs["errors"]).lower()


def test_reply_send_fails_visibly_on_missing_card_context():
    """A pre-S-11 card with no inbound_id/client_id in its value must fail
    visibly, never silently send nothing."""
    body, view = _submit_body(inbound_id=None, client_id=None)
    ack = AsyncMock()
    _run(handle_reply_thread_modal_submit(ack=ack, body=body, view=view, client=AsyncMock()))
    assert ack.await_args.kwargs.get("response_action") == "errors"


def test_reply_send_capped_mailbox_surfaces_as_modal_error():
    row = _inbound_row(status="PENDING")
    session = _fake_session(inbound_row=row)
    body, view = _submit_body()
    ack = AsyncMock()
    with patch("src.services.slack.listeners.approver_authorized", return_value=True), \
         patch("src.services.slack.listeners.get_db_context", _fake_ctx(session)), \
         patch("src.services.slack.listeners.get_active_mailbox_for_client", side_effect=AllMailboxesCapped("capped")):
        _run(handle_reply_thread_modal_submit(ack=ack, body=body, view=view, client=AsyncMock()))
    assert ack.await_args.kwargs.get("response_action") == "errors"
    assert "capacity" in str(ack.await_args.kwargs["errors"]).lower()


# ── handle_book_meeting ──────────────────────────────────────────────────

def test_book_meeting_sends_real_link_when_resolved():
    row = _inbound_row(status="PENDING")
    session = _fake_session(inbound_row=row)
    respond = AsyncMock()
    action = {"value": json.dumps({"inbound_id": "inbound-1", "contact_id": 7, "client_id": "acme_pm"})}
    body = {"user": {"id": "U1"}}
    fake_sender = MagicMock()
    fake_link = SimpleNamespace(url="https://cal.example.com/book/rep", prefilled=False)

    with patch("src.services.slack.listeners.approver_authorized", return_value=True), \
         patch("src.services.slack.listeners.get_db_context", _fake_ctx(session)), \
         patch("src.services.slack.listeners.resolve_booking_link", return_value=fake_link), \
         patch("src.services.slack.listeners.get_active_mailbox_for_client", return_value=_mailbox()), \
         patch("src.services.slack.listeners.build_email_sender", return_value=fake_sender), \
         patch("src.services.slack.listeners.unsubscribe_url", return_value="https://x/unsub"), \
         patch("src.services.slack.listeners.append_unsubscribe_footer", side_effect=lambda b, u: b), \
         patch("src.services.slack.listeners._shared_log_event") as mock_log:
        _run(handle_book_meeting(ack=AsyncMock(), body=body, respond=respond, action=action))

    fake_sender.send.assert_called_once()
    assert "https://cal.example.com/book/rep" in fake_sender.send.call_args.kwargs["body"]
    assert mock_log.call_args.args[1] == "sales_meeting_link_sent"
    respond.assert_awaited_once()
    assert "sent" in respond.await_args.kwargs["text"].lower()


def test_book_meeting_fails_visibly_when_no_default_connection_flagged():
    """resolve_booking_link() returning None (no operator-flagged default
    sales-booking connection — a pre-existing, separate gap) must surface
    as a visible ephemeral error, never a silent no-op."""
    row = _inbound_row(status="PENDING")
    session = _fake_session(inbound_row=row)
    respond = AsyncMock()
    action = {"value": json.dumps({"inbound_id": "inbound-1", "contact_id": 7, "client_id": "acme_pm"})}
    body = {"user": {"id": "U1"}}
    fake_sender = MagicMock()

    with patch("src.services.slack.listeners.approver_authorized", return_value=True), \
         patch("src.services.slack.listeners.get_db_context", _fake_ctx(session)), \
         patch("src.services.slack.listeners.resolve_booking_link", return_value=None), \
         patch("src.services.slack.listeners.build_email_sender", return_value=fake_sender):
        _run(handle_book_meeting(ack=AsyncMock(), body=body, respond=respond, action=action))

    fake_sender.send.assert_not_called()
    respond.assert_awaited_once()
    assert "no default sales-booking" in respond.await_args.kwargs["text"].lower()


def test_book_meeting_rejects_unauthorized_user():
    respond = AsyncMock()
    action = {"value": json.dumps({"inbound_id": "inbound-1", "contact_id": 7, "client_id": "acme_pm"})}
    body = {"user": {"id": "U1"}}
    with patch("src.services.slack.listeners.approver_authorized", return_value=False):
        _run(handle_book_meeting(ack=AsyncMock(), body=body, respond=respond, action=action))
    respond.assert_awaited_once()
    assert "not authorized" in respond.await_args.kwargs["text"].lower()
