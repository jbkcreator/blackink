"""Tests for the 2.1.1 routing gap completions.

Covers:
  - inbound_reply_classified event written after every classification
  - LEGAL_GRIEF issues a CLIENT relay halt
  - COMPLAINT suppresses the sender domain + posts to #blackink-qa
  - HOT_LEAD halts the active sequence run
  - QUESTION confidence < 0.90 sets requires_human_review = TRUE
  - QUESTION confidence >= 0.90 does NOT set requires_human_review
  - PARTNER posts to #client-growth
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from src.agents.respond.classifier import ClassificationResult
from src.agents.respond.intents import Intent
from src.agents.respond import worker
from src.agents.respond.worker import _process_message, _route
from src.agents.respond.queue import InboundQueueMessage


@pytest.fixture(autouse=True)
def _reset_fallback_alert_cooldown():
	"""_should_alert_fallback (PR #48 review finding) keeps process-local
	module state (_last_fallback_alert_at / _fallback_suppressed_since_last_alert)
	so a real outage coalesces into one alert instead of one per message.
	That same state would otherwise leak between test functions in this
	file, since they all import and call the same module — reset it before
	every test regardless of whether the test touches fallback behavior."""
	worker._last_fallback_alert_at = None
	worker._fallback_suppressed_since_last_alert = 0
	yield


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _result(intent: Intent, confidence: float = 0.95, subtype: str | None = None) -> ClassificationResult:
    return ClassificationResult(
        intent=intent,
        confidence=confidence,
        reasoning="test",
        objection_subtype=subtype,
        meta={"path": "llm"},
    )


def _mock_db() -> MagicMock:
    db = MagicMock()
    db.execute.return_value = MagicMock()
    return db


def _sql_text_of_call(call) -> str:
    """Extract SQL string from a mock db.execute call's first positional arg.

    SQLAlchemy TextClause.__str__ returns the SQL text, so str(arg) works.
    """
    return str(call[0][0])


# ---------------------------------------------------------------------------
# inbound_reply_classified event
# ---------------------------------------------------------------------------

class TestInboundReplyClassifiedEvent:
    def test_event_written_for_hot_lead(self):
        with patch("src.agents.respond.worker.Database") as MockDB, \
             patch("src.agents.respond.worker.classify", return_value=_result(Intent.HOT_LEAD)), \
             patch("src.agents.respond.worker._route", return_value="ROUTED"), \
             patch("src.agents.respond.worker.log_event") as mock_log:

            session = MagicMock()
            session.__enter__ = MagicMock(return_value=session)
            session.__exit__ = MagicMock(return_value=False)
            row = {
                "id": 1, "client_id": "CL1", "sender_email": "a@b.com",
                "subject": "", "body_text": "ready to sign", "status": "PENDING",
                "received_at": datetime.now(timezone.utc),
            }
            session.execute.return_value.mappings.return_value.first.return_value = row
            MockDB.return_value.system_session_scope.return_value = session

            msg = InboundQueueMessage(message_id="0-0", db_id=1, client_id="CL1", idempotency_key="k")
            with patch("src.agents.respond.worker.queue.ack"):
                _process_message(msg)

        mock_log.assert_called_once()
        args, kwargs = mock_log.call_args
        assert args[1] == "inbound_reply_classified"
        assert kwargs["payload"]["detected_intent"] == "HOT_LEAD"
        assert "confidence_score" in kwargs["payload"]

    def test_event_written_for_unsubscribe(self):
        with patch("src.agents.respond.worker.Database") as MockDB, \
             patch("src.agents.respond.worker.classify", return_value=_result(Intent.UNSUBSCRIBE, confidence=1.0)), \
             patch("src.agents.respond.worker._route", return_value="SUPPRESSED"), \
             patch("src.agents.respond.worker.log_event") as mock_log:

            session = MagicMock()
            session.__enter__ = MagicMock(return_value=session)
            session.__exit__ = MagicMock(return_value=False)
            row = {
                "id": 2, "client_id": "CL1", "sender_email": "a@b.com",
                "subject": "", "body_text": "unsubscribe", "status": "PENDING",
                "received_at": datetime.now(timezone.utc),
            }
            session.execute.return_value.mappings.return_value.first.return_value = row
            MockDB.return_value.system_session_scope.return_value = session

            msg = InboundQueueMessage(message_id="0-0", db_id=2, client_id="CL1", idempotency_key="k")
            with patch("src.agents.respond.worker.queue.ack"):
                _process_message(msg)

        mock_log.assert_called_once()
        assert mock_log.call_args[0][1] == "inbound_reply_classified"


# ---------------------------------------------------------------------------
# LEGAL_GRIEF → relay halt
# ---------------------------------------------------------------------------

class TestLegalGriefHalt:
    def _call_route(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None), \
             patch("src.agents.relay.halt_service.issue_halt") as mock_halt:
            _route(
                db=db, db_id=10, client_id="CL1",
                sender_email="lawyer@firm.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.LEGAL_GRIEF, confidence=1.0),
            )
        return mock_halt

    def test_relay_halt_issued(self):
        mock_halt = self._call_route()
        mock_halt.assert_called_once()
        kwargs = mock_halt.call_args[1]
        assert kwargs["scope"] == "CLIENT"
        assert kwargs["scope_id"] == "CL1"
        assert kwargs["issued_by"] == "respond_worker"

    def test_halt_scope_is_client_not_global(self):
        mock_halt = self._call_route()
        assert mock_halt.call_args[1]["scope"] == "CLIENT"

    def test_halt_failure_does_not_raise(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None), \
             patch("src.agents.relay.halt_service.issue_halt", side_effect=RuntimeError("redis down")):
            # Must not raise — alert still fires
            _route(
                db=db, db_id=11, client_id="CL1",
                sender_email="x@y.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.LEGAL_GRIEF, confidence=1.0),
            )


# ---------------------------------------------------------------------------
# COMPLAINT → domain suppress + #blackink-qa
# ---------------------------------------------------------------------------

class TestComplaintRouting:
    def test_domain_suppressed(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None), \
             patch("src.services.email_suppression.suppress_by_domain") as mock_sup:
            _route(
                db=db, db_id=20, client_id="CL1",
                sender_email="angry@owner-corp.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.COMPLAINT),
            )
        mock_sup.assert_called_once()
        # worker calls: suppress_by_domain(db, domain, reason="inbound_complaint")
        # so positional has (db, domain), keyword has {"reason": ...}
        _, domain = mock_sup.call_args[0]
        assert domain == "owner-corp.com"
        assert mock_sup.call_args[1]["reason"] == "inbound_complaint"

    def test_qa_channel_alerted(self):
        db = _mock_db()
        alerted_channels: list[str] = []

        async def fake_post_alert(channel_key, text_body):
            alerted_channels.append(channel_key)

        with patch("src.agents.respond.worker.asyncio.run") as mock_run, \
             patch("src.services.email_suppression.suppress_by_domain"):
            def run_coro(coro):
                import asyncio as _asyncio
                return _asyncio.get_event_loop().run_until_complete(coro) if hasattr(coro, '__await__') else None
            mock_run.side_effect = lambda coro: None

            _route(
                db=db, db_id=20, client_id="CL1",
                sender_email="angry@owner-corp.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.COMPLAINT),
            )

        # Verify asyncio.run was called (the Slack call)
        assert mock_run.call_count >= 1

    def test_no_domain_suppression_if_no_at_sign(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None), \
             patch("src.services.email_suppression.suppress_by_domain") as mock_sup:
            _route(
                db=db, db_id=21, client_id="CL1",
                sender_email="no-at-sign",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.COMPLAINT),
            )
        mock_sup.assert_not_called()


# ---------------------------------------------------------------------------
# HOT_LEAD → sequence halt
# ---------------------------------------------------------------------------

class TestHotLeadSequenceHalt:
    def test_sequence_halted(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            _route(
                db=db, db_id=30, client_id="CL1",
                sender_email="Owner@BigPortfolio.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.HOT_LEAD),
            )
        # _halt_sequence calls db.execute(text("...HALTED..."), params)
        # SQLAlchemy TextClause.__str__ returns the SQL string
        assert any(
            "HALTED" in _sql_text_of_call(c)
            for c in db.execute.call_args_list
        ), "Expected HALTED in a db.execute SQL text"

    def test_halt_uses_lowercased_email(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            _route(
                db=db, db_id=31, client_id="CL1",
                sender_email="Owner@BigPortfolio.COM",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.HOT_LEAD),
            )
        halt_call = next(
            c for c in db.execute.call_args_list
            if "HALTED" in _sql_text_of_call(c)
        )
        params = halt_call[0][1]
        assert params["email"] == "owner@bigportfolio.com"


# ---------------------------------------------------------------------------
# QUESTION → requires_human_review
# ---------------------------------------------------------------------------

class TestQuestionHumanReview:
    def test_low_confidence_sets_human_review(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            _route(
                db=db, db_id=40, client_id="CL1",
                sender_email="q@owner.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.QUESTION, confidence=0.75),
            )
        write_call = next(
            c for c in db.execute.call_args_list
            if "requires_human_review" in _sql_text_of_call(c)
        )
        params = write_call[0][1]
        assert params["human_review"] is True

    def test_high_confidence_does_not_set_human_review(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            _route(
                db=db, db_id=41, client_id="CL1",
                sender_email="q@owner.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.QUESTION, confidence=0.92),
            )
        write_call = next(
            c for c in db.execute.call_args_list
            if "requires_human_review" in _sql_text_of_call(c)
        )
        params = write_call[0][1]
        assert params["human_review"] is False

    def test_boundary_exactly_090_not_flagged(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            _route(
                db=db, db_id=42, client_id="CL1",
                sender_email="q@owner.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.QUESTION, confidence=0.90),
            )
        write_call = next(
            c for c in db.execute.call_args_list
            if "requires_human_review" in _sql_text_of_call(c)
        )
        assert write_call[0][1]["human_review"] is False


# ---------------------------------------------------------------------------
# PARTNER → #client-growth
# ---------------------------------------------------------------------------

class TestPartnerRouting:
    def test_client_growth_alerted(self):
        db = _mock_db()
        run_calls: list = []
        with patch("src.agents.respond.worker.asyncio.run") as mock_run:
            mock_run.side_effect = lambda coro: run_calls.append(coro) or None
            _route(
                db=db, db_id=50, client_id="CL1",
                sender_email="agent@realty.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.PARTNER),
            )
        assert mock_run.called, "Expected asyncio.run to be called for PARTNER"

    def test_non_partner_does_not_alert_client_growth(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None) as mock_run:
            _route(
                db=db, db_id=51, client_id="CL1",
                sender_email="owner@co.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.NURTURE),
            )
        # NURTURE produces no Slack call at all
        assert mock_run.call_count == 0


# ---------------------------------------------------------------------------
# Group D / D-10 — classifier fallback (meta={"path": "fallback"}) must never
# be silently indistinguishable from a genuine NURTURE classification: it
# must set requires_human_review, alert a human, and must NOT halt the
# sequence (the true intent is unknown — halting on every transient LLM
# error would stall live campaigns for no reason).
# ---------------------------------------------------------------------------

def _fallback_result() -> ClassificationResult:
    return ClassificationResult(
        intent=Intent.NURTURE,
        confidence=0.0,
        reasoning="Classifier error — conservative fallback pending manual review.",
        meta={"path": "fallback"},
    )


class TestClassifierFallback:
    def test_fallback_sets_requires_human_review(self):
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            _route(
                db=db, db_id=60, client_id="CL1",
                sender_email="owner@co.com",
                received_at=datetime.now(timezone.utc),
                result=_fallback_result(),
            )
        write_call = next(
            c for c in db.execute.call_args_list
            if "requires_human_review" in _sql_text_of_call(c)
        )
        assert write_call[0][1]["human_review"] is True

    def test_fallback_fires_a_slack_alert(self):
        """Without this alert the row is permanently invisible: NURTURE gets
        no context card (not in CONTEXT_CARD_INTENTS) and the SLA sweep can
        only escalate a row that already has a card (card_ts IS NOT NULL)."""
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None) as mock_run:
            _route(
                db=db, db_id=61, client_id="CL1",
                sender_email="owner@co.com",
                received_at=datetime.now(timezone.utc),
                result=_fallback_result(),
            )
        assert mock_run.called, "Expected asyncio.run to be called for a fallback result"

    def test_fallback_does_not_halt_sequence(self):
        """The true intent is unknown on a classifier error — halting on
        every transient LLM failure would stall live campaigns for no
        reason. A human reviewer halts it if the reply actually warrants it."""
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            _route(
                db=db, db_id=62, client_id="CL1",
                sender_email="owner@co.com",
                received_at=datetime.now(timezone.utc),
                result=_fallback_result(),
            )
        halt_calls = [c for c in db.execute.call_args_list if "HALTED" in _sql_text_of_call(c)]
        assert halt_calls == []

    def test_genuine_nurture_does_not_set_requires_human_review(self):
        """Control: a real LLM-classified NURTURE (meta={"path": "llm"}) must
        NOT be flagged for human review — only the fallback path is."""
        db = _mock_db()
        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            _route(
                db=db, db_id=63, client_id="CL1",
                sender_email="owner@co.com",
                received_at=datetime.now(timezone.utc),
                result=_result(Intent.NURTURE),
            )
        write_call = next(
            c for c in db.execute.call_args_list
            if "requires_human_review" in _sql_text_of_call(c)
        )
        assert write_call[0][1]["human_review"] is False


# ---------------------------------------------------------------------------
# PR #48 review finding — the fallback Slack alert must coalesce during a
# sustained outage (every queued reply hitting the classifier fallback path
# at once), not post one synchronous alert per message.
# ---------------------------------------------------------------------------

class TestClassifierFallbackAlertCooldown:
    def test_second_fallback_within_cooldown_does_not_alert(self):
        """Two fallback messages back-to-back (well inside the 5-minute
        cooldown) must produce exactly one Slack post, not two."""
        with patch("src.agents.respond.worker.asyncio.run", return_value=None) as mock_run:
            for db_id in (70, 71):
                db = _mock_db()
                _route(
                    db=db, db_id=db_id, client_id="CL1",
                    sender_email="owner@co.com",
                    received_at=datetime.now(timezone.utc),
                    result=_fallback_result(),
                )
        assert mock_run.call_count == 1

    def test_requires_human_review_still_set_on_every_suppressed_fallback(self):
        """The cooldown throttles the ALERT only — every affected row must
        still be flagged for human review regardless of whether its alert
        was suppressed, so the Respond queue itself never silently drops a
        row during a sustained outage."""
        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            dbs = []
            for db_id in (72, 73, 74):
                db = _mock_db()
                dbs.append(db)
                _route(
                    db=db, db_id=db_id, client_id="CL1",
                    sender_email="owner@co.com",
                    received_at=datetime.now(timezone.utc),
                    result=_fallback_result(),
                )
        for db in dbs:
            write_call = next(
                c for c in db.execute.call_args_list
                if "requires_human_review" in _sql_text_of_call(c)
            )
            assert write_call[0][1]["human_review"] is True

    def test_many_consecutive_fallbacks_produce_at_most_one_alert(self):
        """Simulates a sustained outage: N consecutive fallback
        classifications must coalesce into at most one Slack post, not N."""
        with patch("src.agents.respond.worker.asyncio.run", return_value=None) as mock_run:
            for db_id in range(80, 130):  # 50 consecutive fallbacks
                db = _mock_db()
                _route(
                    db=db, db_id=db_id, client_id="CL1",
                    sender_email="owner@co.com",
                    received_at=datetime.now(timezone.utc),
                    result=_fallback_result(),
                )
        assert mock_run.call_count <= 1

    def test_alert_after_cooldown_expires_reports_suppressed_count(self):
        """Once the cooldown window has elapsed, the next fallback alert
        must fire again — and its text must report how many were
        suppressed in between, so an outage reads as "N suppressed" rather
        than a single unlabeled ping that hides its own scale."""
        from src.agents.respond import worker as worker_mod

        with patch("src.agents.respond.worker._post_slack_alert", new_callable=AsyncMock) as mock_alert:
            db1 = _mock_db()
            _route(
                db=db1, db_id=90, client_id="CL1",
                sender_email="owner@co.com",
                received_at=datetime.now(timezone.utc),
                result=_fallback_result(),
            )
            # Two suppressed while still inside the cooldown.
            for db_id in (91, 92):
                db = _mock_db()
                _route(
                    db=db, db_id=db_id, client_id="CL1",
                    sender_email="owner@co.com",
                    received_at=datetime.now(timezone.utc),
                    result=_fallback_result(),
                )
            assert mock_alert.await_count == 1

            # Force the cooldown to have elapsed.
            worker_mod._last_fallback_alert_at = (
                datetime.now(timezone.utc)
                - timedelta(seconds=worker_mod._FALLBACK_ALERT_COOLDOWN_SECONDS + 1)
            )
            db2 = _mock_db()
            _route(
                db=db2, db_id=93, client_id="CL1",
                sender_email="owner@co.com",
                received_at=datetime.now(timezone.utc),
                result=_fallback_result(),
            )
        assert mock_alert.await_count == 2
        second_alert_text = mock_alert.await_args_list[1].args[1]
        assert "2 more suppressed" in second_alert_text

    def test_suppressed_count_resets_after_posting(self):
        """After a cooldown-expired alert posts, the suppressed counter must
        reset to zero — a THIRD alert (after a second cooldown expiry) must
        not double-count suppressions from the first outage window."""
        from src.agents.respond import worker as worker_mod

        with patch("src.agents.respond.worker.asyncio.run", return_value=None):
            db = _mock_db()
            _route(
                db=db, db_id=100, client_id="CL1", sender_email="owner@co.com",
                received_at=datetime.now(timezone.utc), result=_fallback_result(),
            )
            assert worker_mod._fallback_suppressed_since_last_alert == 0

            for db_id in (101, 102, 103):
                db = _mock_db()
                _route(
                    db=db, db_id=db_id, client_id="CL1", sender_email="owner@co.com",
                    received_at=datetime.now(timezone.utc), result=_fallback_result(),
                )
            assert worker_mod._fallback_suppressed_since_last_alert == 3

            worker_mod._last_fallback_alert_at = (
                datetime.now(timezone.utc)
                - timedelta(seconds=worker_mod._FALLBACK_ALERT_COOLDOWN_SECONDS + 1)
            )
            db = _mock_db()
            _route(
                db=db, db_id=104, client_id="CL1", sender_email="owner@co.com",
                received_at=datetime.now(timezone.utc), result=_fallback_result(),
            )
            assert worker_mod._fallback_suppressed_since_last_alert == 0
