"""Tests for src.services.inbound_ingest.ingest_inbound_reply (Task 3.1.3).

The service owns the BYPASSRLS system session and the full ingestion flow:
client resolution, BCC-loop break, dedup, attribution, persistence, and the
#sales-replies Slack card. These tests patch the collaborators and assert the
discard reasons and the happy-path store+post — mirroring the trust-boundary
test style used elsewhere (FakeSession + AsyncMock for Slack).
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.inbound_ingest import InboundParsed, ingest_inbound_reply


def _parsed(**overrides) -> InboundParsed:
    base = dict(
        to_alias="testclient@inbound.getblackink.com",
        from_raw="prospect@example.com",
        subject="Re: Your Visibility Report",
        raw_body="Thanks!",
        inbound_message_id="<inbound-1@mail.example.com>",
        in_reply_to="<msg-id-123@mail.example.com>",
    )
    base.update(overrides)
    return InboundParsed(**base)


def _fake_session(existing=False, inserted_id=42, dispatch_id=None):
    """Call-aware fake: distinguishes the dedup SELECT from the INSERT ...
    RETURNING id (both go through session.execute(), so a single static
    return_value can't represent both — a real bug-fix regression test, not
    incidental mock plumbing: this is exactly the distinction
    ingest_inbound_reply's own duplicate-vs-inserted branch depends on)."""
    session = MagicMock()

    def _execute(stmt, params=None, *a, **kw):
        sql = str(stmt)
        result = MagicMock()
        if "SELECT id FROM inbound_messages WHERE idempotency_key" in sql:
            result.first.return_value = MagicMock() if existing else None
        elif "INSERT INTO inbound_messages" in sql:
            row = MagicMock()
            row.id = inserted_id
            result.first.return_value = row
        elif "FROM sequence_touch_dispatches" in sql:
            if dispatch_id is not None:
                row = MagicMock()
                row.dispatch_id = dispatch_id
                result.first.return_value = row
            else:
                result.first.return_value = None
        else:
            result.first.return_value = None
        # _recent_thread calls .mappings().all() — return empty so it renders no thread
        result.mappings.return_value.all.return_value = []
        # fetch_latest_ovs guards on to_regclass(...).scalar() — None ⇒ OVS table absent
        result.scalar.return_value = None
        return result

    session.execute.side_effect = _execute
    return session


def _patches(
    *,
    client_id="testclient",
    is_echo=False,
    existing=False,
    attribution_status="attributed",
    contact_id=1,
    dispatch_id=None,
):
    session = _fake_session(existing=existing, dispatch_id=dispatch_id)
    cm = MagicMock()
    cm.__enter__ = lambda s: session
    cm.__exit__ = MagicMock(return_value=False)

    attribution = MagicMock()
    attribution.client_id = client_id or "testclient"
    attribution.contact_id = contact_id
    attribution.run_id = "run-uuid-1" if attribution_status == "attributed" else None
    attribution.touch_step = 1 if attribution_status == "attributed" else None
    attribution.attribution_status = attribution_status
    attribution.contact_name = "Jane Doe" if contact_id else None
    attribution.firm_name = "Acme PM" if contact_id else None
    attribution.firm_domain = "acmepm.com" if contact_id else None
    attribution.firm_company_id = "co-1" if contact_id else None
    attribution.door_count = 120 if contact_id else None

    return (
        patch("src.services.inbound_ingest.resolve_client_from_alias", return_value=client_id),
        patch("src.services.inbound_ingest.is_bcc_echo", return_value=is_echo),
        patch("src.services.inbound_ingest.attribute", return_value=attribution),
        patch("src.services.inbound_ingest.get_system_db_context", return_value=cm),
        patch("src.services.inbound_ingest.slack_post.post_notice", new_callable=AsyncMock),
    )


def _run(parsed):
    return asyncio.run(ingest_inbound_reply(parsed))


def test_unknown_alias_discards():
    p1, p2, p3, p4, p5 = _patches(client_id=None)
    with p1, p2, p3, p4, p5 as mock_post:
        result = _run(_parsed())
    assert result == {"status": "discarded", "reason": "unknown_alias"}
    mock_post.assert_not_awaited()


def test_bcc_echo_discards():
    p1, p2, p3, p4, p5 = _patches(is_echo=True)
    with p1, p2, p3, p4, p5 as mock_post:
        result = _run(_parsed())
    assert result["reason"] == "bcc_echo"
    mock_post.assert_not_awaited()


def test_duplicate_message_id_discards():
    p1, p2, p3, p4, p5 = _patches(existing=True)
    with p1, p2, p3, p4, p5 as mock_post:
        result = _run(_parsed())
    assert result["reason"] == "duplicate"
    mock_post.assert_not_awaited()


def test_attributed_reply_stored_and_posted():
    p1, p2, p3, p4, p5 = _patches(attribution_status="attributed", contact_id=42)
    with p1, p2, p3, p4, p5 as mock_post:
        result = _run(_parsed())
    assert result["status"] == "ok"
    assert "inbound_id" in result
    mock_post.assert_awaited_once()
    assert mock_post.await_args.kwargs["channel_key"] == "replies"


def test_unattributed_reply_still_posted():
    p1, p2, p3, p4, p5 = _patches(attribution_status="unattributed", contact_id=None)
    with p1, p2, p3, p4, p5 as mock_post:
        result = _run(_parsed())
    assert result["status"] == "ok"
    mock_post.assert_awaited_once()


def test_thread_history_posted_as_threaded_replies():
    """Prior messages post as replies under the card (thread_ts set), oldest→newest."""
    p1, p2, p3, p4, p5 = _patches(attribution_status="attributed", contact_id=7)
    with p1, p2, p3, p4, p5 as mock_post, \
         patch("src.services.inbound_ingest._recent_thread", return_value=["⬅️ newest", "➡️ oldest"]):
        mock_post.return_value = "1700000000.0001"  # card ts
        _run(_parsed())
    threaded = [c for c in mock_post.await_args_list if c.kwargs.get("thread_ts")]
    assert len(threaded) == 2  # two history lines threaded under the card
    # oldest posted first (we reverse newest-first list)
    assert "oldest" in threaded[0].kwargs["text"]


def test_display_name_stripped_for_from_address():
    p1, p2, p3, p4, p5 = _patches()
    with p1, p2, p3, p4, p5 as mock_post:
        _run(_parsed(from_raw="Jane Doe <jane@acme.com>"))
    # Card is posted as a colored attachment; from_address is the bare email.
    assert mock_post.await_args.kwargs["attachments"][0]["blocks"] is not None
    assert "jane@acme.com" in mock_post.await_args.kwargs["text"]


# ── S-8: email_replied producer ──────────────────────────────────────────

def test_tier1_attributed_reply_with_resolvable_dispatch_writes_email_replied():
    p1, p2, p3, p4, p5 = _patches(attribution_status="attributed", contact_id=7, dispatch_id="dispatch-99")
    with p1, p2, p3, p4, p5 as mock_post, \
         patch("src.services.inbound_ingest.log_event") as mock_log:
        _run(_parsed())
    email_replied_calls = [c for c in mock_log.call_args_list if c.args[1] == "email_replied"]
    assert len(email_replied_calls) == 1
    assert email_replied_calls[0].kwargs["payload"] == {"dispatch_id": "dispatch-99"}


def test_tier1_attributed_reply_with_no_matching_sent_dispatch_writes_no_email_replied():
    """run_id/touch_step present but no SENT dispatch row found (e.g. the
    touch never actually sent) — must not fabricate a dispatch_id."""
    p1, p2, p3, p4, p5 = _patches(attribution_status="attributed", contact_id=7, dispatch_id=None)
    with p1, p2, p3, p4, p5, \
         patch("src.services.inbound_ingest.log_event") as mock_log:
        _run(_parsed())
    email_replied_calls = [c for c in mock_log.call_args_list if c.args[1] == "email_replied"]
    assert email_replied_calls == []


def test_tier2_unattributed_reply_writes_no_email_replied():
    """Sender-email-only match has no run_id/touch_step — no dispatch to
    attribute the reply to, so it must not count toward the digest's
    reply-rate metric, even though it still posts to #sales-replies."""
    p1, p2, p3, p4, p5 = _patches(attribution_status="unattributed", contact_id=None)
    with p1, p2, p3, p4, p5 as mock_post, \
         patch("src.services.inbound_ingest.log_event") as mock_log:
        result = _run(_parsed())
    assert result["status"] == "ok"
    mock_post.assert_awaited_once()  # still posts to #sales-replies
    email_replied_calls = [c for c in mock_log.call_args_list if c.args[1] == "email_replied"]
    assert email_replied_calls == []


def test_email_replied_is_deduped_per_dispatch_id():
    """Code-review fix: a SECOND reply attributed to the same dispatch_id
    (e.g. the prospect replies twice in the same thread) must not double-
    write email_replied — the exact inflation class email_opened/
    email_clicked already guard against."""
    p1, p2, p3, p4, p5 = _patches(attribution_status="attributed", contact_id=7, dispatch_id="dispatch-99")
    with p1, p2, p3, p4, p5, \
         patch("src.services.inbound_ingest.log_event") as mock_log, \
         patch("src.services.inbound_ingest.already_logged_for_dispatch", return_value=True) as mock_dedup:
        _run(_parsed())
    mock_dedup.assert_called_once()
    assert mock_dedup.call_args.args[1:] == ("testclient", "email_replied", "dispatch-99")
    email_replied_calls = [c for c in mock_log.call_args_list if c.args[1] == "email_replied"]
    assert email_replied_calls == []
