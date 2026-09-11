"""Tests that an inbound UNSUBSCRIBE reply atomically suppresses the contact.

The suppression must land in the same DB transaction as the SUPPRESSED status
write — a crash between the two must not leave the contact reachable.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.respond.classifier import ClassificationResult
from src.agents.respond.intents import Intent
from src.agents.respond.worker import _route


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_db(suppressed_after: list):
    """Return a mock DB session that records suppress_by_email calls."""
    db = MagicMock()
    db.execute.return_value.mappings.return_value.first.return_value = None
    # Capture calls to suppress_by_email via the db session
    db.commit = MagicMock()
    return db


def _unsubscribe_result() -> ClassificationResult:
    return ClassificationResult(
        intent=Intent.UNSUBSCRIBE,
        confidence=1.0,
        reasoning="deterministic",
        objection_subtype=None,
        meta={},
    )


# ---------------------------------------------------------------------------
# Core behaviour
# ---------------------------------------------------------------------------

def test_unsubscribe_calls_suppress_before_commit():
    """suppress_by_email must be called before db.commit() — not after."""
    call_order: list[str] = []

    db = MagicMock()
    db.execute.return_value = MagicMock()  # _write_result executes fine

    def track_commit():
        call_order.append("commit")

    db.commit.side_effect = track_commit

    with patch("src.agents.respond.worker._post_slack_alert", new=AsyncMock()), \
         patch("src.agents.respond.worker.asyncio.run"), \
         patch("src.services.email_suppression.suppress_by_email") as mock_suppress, \
         patch("src.services.email_suppression.suppress_by_domain"):

        def track_suppress(session, email, reason):
            call_order.append("suppress")

        mock_suppress.side_effect = track_suppress

        _route(
            db=db,
            db_id=1,
            client_id="TEST_CLIENT",
            sender_email="owner@example.com",
            received_at=datetime.now(timezone.utc),
            result=_unsubscribe_result(),
        )

    assert call_order.index("suppress") < call_order.index("commit"), (
        "suppress_by_email must be called before db.commit()"
    )


def test_unsubscribe_passes_correct_email_and_reason():
    db = MagicMock()
    db.execute.return_value = MagicMock()

    with patch("src.agents.respond.worker._post_slack_alert", new=AsyncMock()), \
         patch("src.agents.respond.worker.asyncio.run"), \
         patch("src.services.email_suppression.suppress_by_email") as mock_suppress, \
         patch("src.services.email_suppression.suppress_by_domain"):

        _route(
            db=db,
            db_id=42,
            client_id="TEST_CLIENT",
            sender_email="owner@example.com",
            received_at=datetime.now(timezone.utc),
            result=_unsubscribe_result(),
        )

    mock_suppress.assert_called_once()
    # suppress_by_email(db, sender_email, reason=...) — reason is a kwarg
    positional = mock_suppress.call_args[0]
    assert positional[1] == "owner@example.com"
    assert mock_suppress.call_args[1]["reason"] == "inbound_opt_out"


def test_non_unsubscribe_does_not_call_suppress():
    db = MagicMock()
    db.execute.return_value = MagicMock()

    hot_lead = ClassificationResult(
        intent=Intent.HOT_LEAD,
        confidence=0.95,
        reasoning="test",
        objection_subtype=None,
        meta={},
    )

    # return_value=None so _write_card_meta is not triggered (no card meta to persist)
    with patch("src.agents.respond.worker.asyncio.run", return_value=None), \
         patch("src.services.email_suppression.suppress_by_email") as mock_suppress, \
         patch("src.services.email_suppression.suppress_by_domain"):

        _route(
            db=db,
            db_id=99,
            client_id="TEST_CLIENT",
            sender_email="owner@example.com",
            received_at=datetime.now(timezone.utc),
            result=hot_lead,
        )

    mock_suppress.assert_not_called()


def test_unsubscribe_suppress_uses_same_session():
    """suppress_by_email must receive the same session as _write_result —
    so both writes commit together."""
    received_sessions: list = []

    db = MagicMock()
    db.execute.return_value = MagicMock()

    with patch("src.agents.respond.worker._post_slack_alert", new=AsyncMock()), \
         patch("src.agents.respond.worker.asyncio.run"), \
         patch("src.services.email_suppression.suppress_by_email") as mock_suppress, \
         patch("src.services.email_suppression.suppress_by_domain"):

        def capture_session(session, email, reason):
            received_sessions.append(session)

        mock_suppress.side_effect = capture_session

        _route(
            db=db,
            db_id=1,
            client_id="TEST_CLIENT",
            sender_email="owner@example.com",
            received_at=datetime.now(timezone.utc),
            result=_unsubscribe_result(),
        )

    assert len(received_sessions) == 1
    assert received_sessions[0] is db, (
        "suppress_by_email must receive the same session object as _write_result"
    )
