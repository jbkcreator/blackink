"""S-10/S-12 wiring tests: LATER pauses the contact until an extracted date
(or flags for human review when no date is extracted), and UNSUBSCRIBE
suppresses the sender's whole domain + writes the proof-ledger event.

The QUESTION-intent KB-matching piece of S-10 collided with Subtask 2.1.3's
own already-merged KB Auto-Response Engine (knowledge_base_entries /
match_kb() / post_kb_card()) once this branch was rebased onto main — that
duplicate implementation (and its tests) was dropped; 2.1.3's own test
suite (tests/test_kb_matcher.py, tests/test_kb_send_path.py) already covers
that surface.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from src.agents.respond.classifier import ClassificationResult
from src.agents.respond.intents import Intent
from src.agents.respond.worker import _route


def _result(intent: Intent, confidence: float = 0.95) -> ClassificationResult:
    return ClassificationResult(
        intent=intent, confidence=confidence, reasoning="test",
        objection_subtype=None, meta={},
    )


def _mock_db():
    db = MagicMock()
    db.execute.return_value = MagicMock()
    return db


def _sql_text_of_call(call) -> str:
    return str(call[0][0])


# ---------------------------------------------------------------------------
# LATER -> reactivation pause
# ---------------------------------------------------------------------------

def test_later_with_extracted_date_pauses_contact():
    db = _mock_db()
    target = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with patch("src.services.reactivation.extract_target_date", return_value=target), \
         patch("src.services.reactivation.pause_contact_until") as mock_pause, \
         patch("src.agents.respond.worker.asyncio.run", return_value=None):
        _route(
            db=db, db_id=70, client_id="CL1", sender_email="owner@co.com",
            received_at=datetime.now(timezone.utc),
            result=_result(Intent.LATER),
            body_text="call me back in June", contact_id=99,
        )
    mock_pause.assert_called_once_with(db, 99, target)
    # requires_human_review stays False when a date WAS extracted.
    write_call = next(
        c for c in db.execute.call_args_list
        if "requires_human_review" in _sql_text_of_call(c)
    )
    assert write_call[0][1]["human_review"] is False


def test_later_with_no_extracted_date_flags_human_review():
    db = _mock_db()
    with patch("src.services.reactivation.extract_target_date", return_value=None), \
         patch("src.services.reactivation.pause_contact_until") as mock_pause, \
         patch("src.agents.respond.worker.asyncio.run", return_value=None):
        _route(
            db=db, db_id=71, client_id="CL1", sender_email="owner@co.com",
            received_at=datetime.now(timezone.utc),
            result=_result(Intent.LATER),
            body_text="not now", contact_id=99,
        )
    mock_pause.assert_not_called()
    write_call = next(
        c for c in db.execute.call_args_list
        if "requires_human_review" in _sql_text_of_call(c)
    )
    assert write_call[0][1]["human_review"] is True


def test_later_with_date_but_no_contact_id_flags_human_review_instead_of_pausing():
    db = _mock_db()
    target = datetime(2026, 6, 1, tzinfo=timezone.utc)
    with patch("src.services.reactivation.extract_target_date", return_value=target), \
         patch("src.services.reactivation.pause_contact_until") as mock_pause, \
         patch("src.agents.respond.worker.asyncio.run", return_value=None):
        _route(
            db=db, db_id=72, client_id="CL1", sender_email="owner@co.com",
            received_at=datetime.now(timezone.utc),
            result=_result(Intent.LATER),
            body_text="call me back in June", contact_id=None,
        )
    mock_pause.assert_not_called()
    write_call = next(
        c for c in db.execute.call_args_list
        if "requires_human_review" in _sql_text_of_call(c)
    )
    assert write_call[0][1]["human_review"] is True


# ---------------------------------------------------------------------------
# S-12 — UNSUBSCRIBE domain suppression + proof-ledger event
# ---------------------------------------------------------------------------

def test_unsubscribe_suppresses_domain_in_addition_to_email():
    db = _mock_db()
    with patch("src.services.email_suppression.suppress_by_email") as mock_email, \
         patch("src.services.email_suppression.suppress_by_domain") as mock_domain, \
         patch("src.agents.respond.worker.asyncio.run", return_value=None):
        _route(
            db=db, db_id=80, client_id="CL1", sender_email="owner@acme-corp.com",
            received_at=datetime.now(timezone.utc),
            result=_result(Intent.UNSUBSCRIBE, confidence=1.0),
        )
    mock_email.assert_called_once()
    mock_domain.assert_called_once()
    assert mock_domain.call_args[0][1] == "acme-corp.com"


def test_unsubscribe_writes_suppression_applied_event():
    db = _mock_db()
    with patch("src.services.email_suppression.suppress_by_email"), \
         patch("src.services.email_suppression.suppress_by_domain"), \
         patch("src.agents.respond.worker.log_event") as mock_log, \
         patch("src.agents.respond.worker.asyncio.run", return_value=None):
        _route(
            db=db, db_id=81, client_id="CL1", sender_email="owner@acme-corp.com",
            received_at=datetime.now(timezone.utc),
            result=_result(Intent.UNSUBSCRIBE, confidence=1.0),
        )
    calls = [c for c in mock_log.call_args_list if c[0][1] == "suppression_applied"]
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == "CL1"
    assert kwargs["payload"]["scope"] == "EMAIL_AND_DOMAIN"
    assert kwargs["payload"]["domain"] == "acme-corp.com"
    assert kwargs["session"] is db
