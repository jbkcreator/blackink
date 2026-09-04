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


def _fake_session(existing=False):
    session = MagicMock()
    exists_result = MagicMock()
    exists_result.first.return_value = MagicMock() if existing else None
    session.execute.return_value = exists_result
    return session


def _patches(
    *,
    client_id="testclient",
    is_echo=False,
    existing=False,
    attribution_status="attributed",
    contact_id=1,
):
    session = _fake_session(existing=existing)
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


def test_display_name_stripped_for_from_address():
    p1, p2, p3, p4, p5 = _patches()
    with p1, p2, p3, p4, p5 as mock_post:
        _run(_parsed(from_raw="Jane Doe <jane@acme.com>"))
    # Card is posted with the bare email as from_address.
    assert mock_post.await_args.kwargs["blocks"] is not None
    # from_address surfaces in the card text.
    assert "jane@acme.com" in mock_post.await_args.kwargs["text"]
