"""Tests for the LinkedIn (Touch 4) path in src/tasks/sequence_sweep.py.

The sweep now posts LINKEDIN_TASK cards on their day-7 due_at, gated by
evaluate_touch_gate: on a compliance block (incl. global opt-out) it skips the
post and records a touch_skipped_compliance event instead. Dial (Touch 2) is
NOT swept — it is event-driven — so there is no dial path to test here.

FakeSession + AsyncMock for Slack; no live DB.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.tasks.sequence_sweep as sweep


def _order(action_class="LINKEDIN_TASK", contact_id="55", client_id="client-x", ts=None):
    return SimpleNamespace(
        action_class=action_class,
        entity_id=contact_id,
        client_id=client_id,
        action_id="act-1",
        slack_message_ts=ts,
        payload={"touch_step": 4, "run_id": "run-1"},
    )


def _db_ctx_returning_contact(contact_row=SimpleNamespace(contact_id=55, company_id="co-1")):
    session = MagicMock()
    exec_result = MagicMock()
    exec_result.fetchone.return_value = contact_row
    session.execute.return_value = exec_result
    cm = MagicMock()
    cm.__enter__ = lambda s: session
    cm.__exit__ = MagicMock(return_value=False)
    return cm, session


def test_linkedin_card_posted_when_gate_ready():
    cm, session = _db_ctx_returning_contact()
    gate = SimpleNamespace(ready=True, blocked_reasons=[])
    with patch("src.core.database.get_db_context", return_value=cm), \
         patch("src.services.compliance_gate.evaluate_touch_gate", return_value=gate), \
         patch("src.services.slack.listeners.post_work_order_card", new_callable=AsyncMock) as mock_post:
        mock_post.return_value = object()  # non-None → posted
        import asyncio
        ok = asyncio.run(sweep._post_due_linkedin_card(_order()))
    assert ok is True
    mock_post.assert_awaited_once()
    assert mock_post.await_args.kwargs["channel_key"] == "setter"
    # linkedin_task_created event logged after a successful post (v2 line 381)
    logged_sql = " ".join(str(c.args[0]) for c in session.execute.call_args_list)
    assert "linkedin_task_created" in logged_sql


def test_linkedin_card_skipped_and_logged_when_gate_blocks():
    cm, session = _db_ctx_returning_contact()
    gate = SimpleNamespace(ready=False, blocked_reasons=["opted_out"])
    with patch("src.core.database.get_db_context", return_value=cm), \
         patch("src.services.compliance_gate.evaluate_touch_gate", return_value=gate), \
         patch("src.services.slack.listeners.post_work_order_card", new_callable=AsyncMock) as mock_post:
        import asyncio
        ok = asyncio.run(sweep._post_due_linkedin_card(_order()))
    assert ok is False
    mock_post.assert_not_awaited()
    # A touch_skipped_compliance event was written and committed.
    inserted_sql = " ".join(str(c.args[0]) for c in session.execute.call_args_list)
    assert "touch_skipped_compliance" in inserted_sql
    session.commit.assert_called_once()


def test_run_sweep_routes_linkedin_and_email(monkeypatch):
    """run_sweep dispatches email orders to _post_due_card and LinkedIn orders
    to _post_due_linkedin_card; dial orders are ignored entirely."""
    batch = [
        _order(action_class="DISPATCH_EMAIL_TOUCH"),
        _order(action_class="LINKEDIN_TASK"),
        _order(action_class="DIAL_TASK"),
    ]
    monkeypatch.setattr(sweep.wo, "due_batch", lambda **kw: batch)
    monkeypatch.setattr(sweep, "_post_due_card", AsyncMock(return_value=True))
    monkeypatch.setattr(sweep, "_post_due_linkedin_card", AsyncMock(return_value=True))

    posted = sweep.run_sweep()
    assert posted == 2  # one email + one linkedin, dial not swept
    sweep._post_due_card.assert_awaited_once()
    sweep._post_due_linkedin_card.assert_awaited_once()


def test_run_sweep_zero_when_only_dial_due(monkeypatch):
    batch = [_order(action_class="DIAL_TASK")]
    monkeypatch.setattr(sweep.wo, "due_batch", lambda **kw: batch)
    assert sweep.run_sweep() == 0
