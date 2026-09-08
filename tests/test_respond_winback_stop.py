"""Tests that an inbound reply from a winback owner stops their active
winback_rows run — regardless of the classified intent.

winback_rows is a standalone table (never joined to contacts/sequence_runs,
see winback_sequencer.py's docstring), so the existing suppress/halt
side-effects in _route() can't reach it — stop_active_winback_runs() is the
only thing that can, and it must be called for every reply, not just
UNSUBSCRIBE/COMPLAINT/HOT_LEAD, or Touch 2/3 keep firing after the owner has
already engaged.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.respond.classifier import ClassificationResult
from src.agents.respond.intents import Intent
from src.agents.respond.worker import _route


def _result(intent: Intent, confidence: float = 0.95) -> ClassificationResult:
    return ClassificationResult(
        intent=intent,
        confidence=confidence,
        reasoning="test",
        objection_subtype=None,
        meta={},
    )


def _run_route(intent: Intent, db=None):
    db = db or MagicMock()
    db.execute.return_value = MagicMock()

    with patch("src.agents.respond.worker._post_slack_alert", new=AsyncMock()), \
         patch("src.agents.respond.worker.asyncio.run", return_value=None), \
         patch("src.services.email_suppression.suppress_by_email"), \
         patch("src.services.email_suppression.suppress_by_domain"), \
         patch("src.services.winback_sequencer.stop_active_winback_runs") as mock_stop:

        _route(
            db=db,
            db_id=1,
            client_id="TEST_CLIENT",
            sender_email="owner@example.com",
            received_at=datetime.now(timezone.utc),
            result=_result(intent),
        )

    return db, mock_stop


def test_unsubscribe_reply_stops_winback_run_with_opt_out_reason():
    db, mock_stop = _run_route(Intent.UNSUBSCRIBE)
    mock_stop.assert_called_once_with(db, "TEST_CLIENT", "owner@example.com", "OPT_OUT")


def test_complaint_reply_stops_winback_run_with_reply_reason():
    db, mock_stop = _run_route(Intent.COMPLAINT)
    mock_stop.assert_called_once_with(db, "TEST_CLIENT", "owner@example.com", "REPLY")


def test_hot_lead_reply_stops_winback_run_with_reply_reason():
    db, mock_stop = _run_route(Intent.HOT_LEAD)
    mock_stop.assert_called_once_with(db, "TEST_CLIENT", "owner@example.com", "REPLY")


def test_ordinary_nurture_reply_still_stops_winback_run():
    """Proves the stop is intent-agnostic — even a routine, non-actionable
    reply must halt Touch 2/3, since a human is now in the loop either way."""
    db, mock_stop = _run_route(Intent.NURTURE)
    mock_stop.assert_called_once_with(db, "TEST_CLIENT", "owner@example.com", "REPLY")


def test_stop_active_winback_runs_called_before_commit():
    call_order: list[str] = []

    db = MagicMock()
    db.execute.return_value = MagicMock()
    db.commit.side_effect = lambda: call_order.append("commit")

    with patch("src.agents.respond.worker._post_slack_alert", new=AsyncMock()), \
         patch("src.agents.respond.worker.asyncio.run", return_value=None), \
         patch("src.services.winback_sequencer.stop_active_winback_runs") as mock_stop:

        mock_stop.side_effect = lambda *a, **kw: call_order.append("stop_winback")

        _route(
            db=db,
            db_id=1,
            client_id="TEST_CLIENT",
            sender_email="owner@example.com",
            received_at=datetime.now(timezone.utc),
            result=_result(Intent.NURTURE),
        )

    assert call_order.index("stop_winback") < call_order.index("commit"), (
        "stop_active_winback_runs must be called before db.commit()"
    )
