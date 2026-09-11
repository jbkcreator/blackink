"""S-20 (W2 §3.2.5 Stage 5) — routing_latency_seconds evidence field on the
inbound_reply_classified event.

Proves the field is computed from received_at (not from when the worker
happened to start), included for every intent, and reflects boundary cases
correctly (a message queued for a while shows the full elapsed time, not
just the worker's own in-process processing time).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from src.agents.respond.classifier import ClassificationResult
from src.agents.respond.intents import Intent
from src.agents.respond.queue import InboundQueueMessage
from src.agents.respond.worker import _process_message


def _result(intent: Intent, confidence: float = 0.95) -> ClassificationResult:
    return ClassificationResult(
        intent=intent, confidence=confidence, reasoning="test",
        objection_subtype=None, meta={"path": "llm"},
    )


def _run(received_at, intent=Intent.HOT_LEAD):
    with patch("src.agents.respond.worker.Database") as MockDB, \
         patch("src.agents.respond.worker.classify", return_value=_result(intent)), \
         patch("src.agents.respond.worker._route", return_value="ROUTED"), \
         patch("src.agents.respond.worker.log_event") as mock_log:

        session = MagicMock()
        session.__enter__ = MagicMock(return_value=session)
        session.__exit__ = MagicMock(return_value=False)
        row = {
            "id": 1, "client_id": "CL1", "contact_id": None, "sender_email": "a@b.com",
            "subject": "", "body_text": "test", "status": "PENDING",
            "received_at": received_at,
        }
        session.execute.return_value.mappings.return_value.first.return_value = row
        MockDB.return_value.system_session_scope.return_value = session

        msg = InboundQueueMessage(message_id="0-0", db_id=1, client_id="CL1", idempotency_key="k")
        with patch("src.agents.respond.worker.queue.ack"):
            _process_message(msg)

    return mock_log


def test_routing_latency_seconds_present_for_hot_lead():
    received_at = datetime.now(timezone.utc) - timedelta(seconds=45)
    mock_log = _run(received_at, Intent.HOT_LEAD)
    payload = mock_log.call_args[1]["payload"]
    assert "routing_latency_seconds" in payload
    assert 44 <= payload["routing_latency_seconds"] <= 60


def test_routing_latency_seconds_present_for_every_intent_not_just_hot_lead():
    """The field costs nothing to record broadly; a digest/Evidence-Packet
    reader is what filters to HOT_LEAD/WHALE_OWNER for the SLA claim."""
    received_at = datetime.now(timezone.utc) - timedelta(seconds=10)
    mock_log = _run(received_at, Intent.NURTURE)
    payload = mock_log.call_args[1]["payload"]
    assert "routing_latency_seconds" in payload


def test_routing_latency_reflects_full_elapsed_time_including_queue_wait():
    """A message that sat in the queue for 5 minutes before this worker even
    started processing it must show ~300s of latency, not near-zero —
    proving the measurement starts at received_at (true intake), not at
    worker-pickup time."""
    received_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    mock_log = _run(received_at, Intent.HOT_LEAD)
    payload = mock_log.call_args[1]["payload"]
    assert payload["routing_latency_seconds"] >= 295


def test_routing_latency_handles_naive_received_at():
    """received_at from a real DB row can come back tz-naive depending on
    the driver — must not crash, and must still measure real elapsed time
    (treated as UTC, matching this worker's existing tz-naive handling
    elsewhere, e.g. _route()'s own received_at_dt normalization)."""
    naive_received_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=20)
    mock_log = _run(naive_received_at, Intent.HOT_LEAD)
    payload = mock_log.call_args[1]["payload"]
    assert 15 <= payload["routing_latency_seconds"] <= 30
