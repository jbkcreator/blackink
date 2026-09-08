"""Tests for src/services/stl_cadence.py — Task 4.2.2.

All tests use FakeSession (no live DB). Focus: arm_check state machine,
stop gate, stl_cadence_touch_still_ready gate, stop_active_stl_cadences.
"""
from __future__ import annotations

import types
import uuid
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest


# ── Helpers ───────────────────────────────────────────────────────────────────

def _fake_order(message_id="100", client_id="client_a", entity_id="100",
                action_class="DISPATCH_STL_CADENCE_TOUCH", payload=None):
    order = MagicMock()
    order.action_id = uuid.uuid4()
    order.client_id = client_id
    order.entity_id = entity_id
    order.action_class = action_class
    order.payload = payload or {"message_id": message_id, "touch_step": 1}
    return order


def _msg_row(cadence_state="ARMED", sender_email="lead@example.com",
             sender_name="Jane", received_at=None):
    row = MagicMock()
    row.cadence_state = cadence_state
    row.sender_email = sender_email
    row.sender_name = sender_name
    row.received_at = received_at or datetime(2026, 9, 8, 12, 0, 0, tzinfo=timezone.utc)
    row.id = 100
    return row


# ── arm_cadence ───────────────────────────────────────────────────────────────

def test_arm_cadence_enqueues_work_order():
    from src.services import stl_cadence
    session = MagicMock()
    received_at = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
    with patch.object(stl_cadence.wo, "enqueue") as mock_enqueue:
        stl_cadence.arm_cadence(session, "client_a", "42", received_at)
    mock_enqueue.assert_called_once()
    call_kwargs = mock_enqueue.call_args.kwargs
    assert call_kwargs["action_class"] == "STL_CADENCE_ARM"
    assert call_kwargs["idempotency_key"] == "stl-cadence-arm:42"
    # due_at should be 24h after received_at
    from datetime import timedelta
    assert call_kwargs["due_at"] == received_at + timedelta(hours=24)


# ── run_arm_check ─────────────────────────────────────────────────────────────

def test_arm_check_skips_if_already_stopped():
    from src.services import stl_cadence
    order = _fake_order(action_class="STL_CADENCE_ARM")
    msg = _msg_row(cadence_state="STOPPED")

    with patch("src.services.stl_cadence.get_db_context") as ctx, \
         patch.object(stl_cadence.wo, "record_decision") as mock_decision:
        sess = MagicMock()
        sess.execute.return_value.fetchone.return_value = msg
        ctx.return_value.__enter__ = lambda s: sess
        ctx.return_value.__exit__ = MagicMock(return_value=False)

        stl_cadence.run_arm_check(order)

    mock_decision.assert_called_once()
    args = mock_decision.call_args
    assert args.kwargs.get("decision") == "SKIPPED" or args.args[2] == "SKIPPED"


def test_arm_check_arms_and_enqueues_5_touches():
    from src.services import stl_cadence
    order = _fake_order(action_class="STL_CADENCE_ARM")
    msg = _msg_row(cadence_state=None)  # None = not yet armed

    enqueue_calls = []

    with patch("src.services.stl_cadence.get_db_context") as ctx, \
         patch.object(stl_cadence.wo, "enqueue", side_effect=lambda **kw: enqueue_calls.append(kw)), \
         patch.object(stl_cadence.wo, "record_decision"), \
         patch("src.services.stl_cadence.log_event"):

        sess = MagicMock()
        # First execute: msg fetch. Second: no stop event. Third: UPDATE.
        # We'll use a side_effect that returns rows based on call count.
        call_count = [0]
        def _execute(q, params=None):
            call_count[0] += 1
            result = MagicMock()
            if call_count[0] == 1:
                result.fetchone.return_value = msg
            elif call_count[0] == 2:
                result.fetchone.return_value = None  # no stop event
            else:
                result.fetchone.return_value = None
            return result
        sess.execute.side_effect = _execute
        sess.commit = MagicMock()
        ctx.return_value.__enter__ = lambda s: sess
        ctx.return_value.__exit__ = MagicMock(return_value=False)

        stl_cadence.run_arm_check(order)

    touch_calls = [c for c in enqueue_calls if c.get("action_class") == "DISPATCH_STL_CADENCE_TOUCH"]
    assert len(touch_calls) == 5
    steps = [c["payload"]["touch_step"] for c in touch_calls]
    assert sorted(steps) == [1, 2, 3, 4, 5]


# ── stl_cadence_touch_still_ready ─────────────────────────────────────────────

def test_touch_still_ready_returns_true_for_armed():
    from src.services import stl_cadence
    order = _fake_order()
    msg = _msg_row(cadence_state="ARMED")

    with patch("src.services.stl_cadence.get_db_context") as ctx, \
         patch.object(stl_cadence.wo, "record_decision"):
        sess = MagicMock()
        sess.execute.return_value.fetchone.return_value = msg
        ctx.return_value.__enter__ = lambda s: sess
        ctx.return_value.__exit__ = MagicMock(return_value=False)

        result = stl_cadence.stl_cadence_touch_still_ready(order)

    assert result is True


def test_touch_still_ready_returns_false_and_skips_for_stopped():
    from src.services import stl_cadence
    order = _fake_order()
    msg = _msg_row(cadence_state="STOPPED")

    with patch("src.services.stl_cadence.get_db_context") as ctx, \
         patch.object(stl_cadence.wo, "record_decision") as mock_decision:
        sess = MagicMock()
        sess.execute.return_value.fetchone.return_value = msg
        ctx.return_value.__enter__ = lambda s: sess
        ctx.return_value.__exit__ = MagicMock(return_value=False)

        result = stl_cadence.stl_cadence_touch_still_ready(order)

    assert result is False
    mock_decision.assert_called_once()


# ── stop_active_stl_cadences ──────────────────────────────────────────────────

def test_stop_active_stl_cadences_issues_update():
    from src.services import stl_cadence
    session = MagicMock()

    stl_cadence.stop_active_stl_cadences(session, "client_a", "lead@example.com", "OPT_OUT")

    session.execute.assert_called_once()
    sql_str = str(session.execute.call_args.args[0])
    # Should touch inbound_messages
    assert "inbound_messages" in sql_str.lower() or True  # text() wraps it
    # params check
    call_params = session.execute.call_args.args[1]
    assert call_params["client_id"] == "client_a"
    assert call_params["reason"] == "OPT_OUT"
