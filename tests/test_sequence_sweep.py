"""Tests for src/tasks/sequence_sweep.py.

Two independent pieces covered here:
  1. The Win-Back gate-before-posting fix (Subtask 3.1.2) — the DoD requires
     a Touch 2/3 card never be QUEUED after a stop, not just blocked when
     someone later clicks Approve.
  2. The LinkedIn (Touch 4) path — the sweep posts LINKEDIN_TASK cards on
     their day-7 due_at, gated by evaluate_touch_gate: on a compliance block
     (incl. global opt-out) it skips the post and records a
     touch_skipped_compliance event instead. Dial (Touch 2) is NOT swept —
     it is event-driven — so there is no dial path to test here.

No live DB or Slack; work_orders and the gates are stubbed.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.tasks import sequence_sweep
from src.tasks import sequence_sweep as sweep


# ---------------------------------------------------------------------------
# Win-Back gate-before-posting (Subtask 3.1.2)
# ---------------------------------------------------------------------------

def _winback_order(action_class="DISPATCH_WINBACK_TOUCH", winback_row_id=1, slack_message_ts=None):
	return SimpleNamespace(
		action_id="action-1",
		client_id="client_a",
		entity_id=str(winback_row_id),
		action_class=action_class,
		slack_message_ts=slack_message_ts,
		payload={"winback_row_id": winback_row_id, "touch_step": 2},
	)


def _gate(ready: bool):
	return SimpleNamespace(ready=ready, blocked_reasons=[] if ready else ["not_stopped: FAIL - stop_reason=REPLY"])


def test_stopped_winback_touch_is_skipped_not_posted():
	order = _winback_order()
	with (
		patch.object(sequence_sweep.wo, "due_batch", return_value=[order]),
		patch("src.services.winback_sequencer.evaluate_winback_touch_gate", return_value=_gate(False)),
		patch("src.core.database.get_db_context") as mock_ctx,
		patch.object(sequence_sweep.wo, "record_decision") as mock_decision,
		patch("src.tasks.sequence_sweep._post_due_card") as mock_post,
	):
		mock_ctx.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = SimpleNamespace(disposition="STILL_OWNS_STILL_RENTING")
		posted = sequence_sweep.run_sweep()
	assert posted == 0
	mock_decision.assert_called_once_with("client_a", "action-1", decision="SKIPPED", decided_by="system:winback_gate")
	mock_post.assert_not_called()


def test_ready_winback_touch_is_posted():
	order = _winback_order()
	with (
		patch.object(sequence_sweep.wo, "due_batch", return_value=[order]),
		patch("src.services.winback_sequencer.evaluate_winback_touch_gate", return_value=_gate(True)),
		patch("src.core.database.get_db_context") as mock_ctx,
		patch.object(sequence_sweep.wo, "record_decision") as mock_decision,
	):
		mock_ctx.return_value.__enter__.return_value.execute.return_value.fetchone.return_value = SimpleNamespace(disposition="STILL_OWNS_STILL_RENTING")
		with patch("asyncio.run", return_value=True) as mock_run:
			posted = sequence_sweep.run_sweep()
	assert posted == 1
	mock_decision.assert_not_called()
	mock_run.assert_called_once()


def test_non_winback_order_is_unaffected_by_gate_check():
	order = _winback_order(action_class="DISPATCH_EMAIL_TOUCH")
	with (
		patch.object(sequence_sweep.wo, "due_batch", return_value=[order]),
		patch("src.services.winback_sequencer.evaluate_winback_touch_gate") as mock_gate,
	):
		with patch("asyncio.run", return_value=True):
			posted = sequence_sweep.run_sweep()
	assert posted == 1
	mock_gate.assert_not_called()


def test_already_posted_card_is_never_re_evaluated():
	order = _winback_order(slack_message_ts="1700000000.000100")
	with (
		patch.object(sequence_sweep.wo, "due_batch", return_value=[order]),
		patch("src.services.winback_sequencer.evaluate_winback_touch_gate") as mock_gate,
	):
		posted = sequence_sweep.run_sweep()
	assert posted == 0
	mock_gate.assert_not_called()


# ---------------------------------------------------------------------------
# LinkedIn (Touch 4) path
# ---------------------------------------------------------------------------

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


# ── PR #26 finding 3: blocked LinkedIn tasks must not loop forever ────────────


def _order_with_created(created_at, contact_id="55", client_id="client-x"):
    o = _order(contact_id=contact_id, client_id=client_id)
    o.created_at = created_at
    return o


def _sql_of(session):
    return " ".join(str(c.args[0]) for c in session.execute.call_args_list)


def _params_of(session):
    return [c.args[1] for c in session.execute.call_args_list if len(c.args) > 1]


def test_permanent_optout_terminally_skips_order():
    """An opted-out contact → the work order is SKIPPED in the same txn as the
    event, so due_batch never re-selects it and never re-logs (finding 3)."""
    contact = SimpleNamespace(contact_id=55, company_id="co-1", is_opted_out=True, suppression_state=False)
    cm, session = _db_ctx_returning_contact(contact_row=contact)
    gate = SimpleNamespace(ready=False, blocked_reasons=["email_verified: FAIL - is_opted_out=true"])
    with patch("src.core.database.get_db_context", return_value=cm), \
         patch("src.services.compliance_gate.evaluate_touch_gate", return_value=gate), \
         patch("src.services.slack.listeners.post_work_order_card", new_callable=AsyncMock) as mock_post:
        import asyncio
        ok = asyncio.run(sweep._post_due_linkedin_card(_order_with_created(datetime.now(timezone.utc))))
    assert ok is False
    mock_post.assert_not_awaited()
    sql = _sql_of(session)
    assert "UPDATE agent_work_orders" in sql and "status = 'SKIPPED'" in sql
    assert "due_at = :next_due" not in sql  # not merely deferred
    assert '"disposition": "SKIPPED_PERMANENT"' in sql or "SKIPPED_PERMANENT" in json_payloads(session)
    session.commit.assert_called_once()


def test_retryable_block_defers_due_at_without_skipping():
    """A non-permanent block on a young order defers due_at (bounded backoff),
    keeping the order QUEUED so it is re-checked at most once per interval."""
    contact = SimpleNamespace(contact_id=55, company_id="co-1", is_opted_out=False, suppression_state=False)
    cm, session = _db_ctx_returning_contact(contact_row=contact)
    gate = SimpleNamespace(ready=False, blocked_reasons=["dnc: ABSTAIN - unknown"])
    with patch("src.core.database.get_db_context", return_value=cm), \
         patch("src.services.compliance_gate.evaluate_touch_gate", return_value=gate), \
         patch("src.services.slack.listeners.post_work_order_card", new_callable=AsyncMock):
        import asyncio
        ok = asyncio.run(sweep._post_due_linkedin_card(_order_with_created(datetime.now(timezone.utc))))
    assert ok is False
    sql = _sql_of(session)
    assert "due_at = :next_due" in sql
    assert "status = 'SKIPPED'" not in sql
    session.commit.assert_called_once()


def test_retryable_block_terminally_skips_once_window_exhausted():
    """A non-permanent block on an order older than the retry window is
    terminally SKIPPED — bounded, never retried forever."""
    contact = SimpleNamespace(contact_id=55, company_id="co-1", is_opted_out=False, suppression_state=False)
    cm, session = _db_ctx_returning_contact(contact_row=contact)
    gate = SimpleNamespace(ready=False, blocked_reasons=["dnc: ABSTAIN - unknown"])
    old = datetime.now(timezone.utc) - sweep._LINKEDIN_MAX_RETRY_AGE - timedelta(hours=1)
    with patch("src.core.database.get_db_context", return_value=cm), \
         patch("src.services.compliance_gate.evaluate_touch_gate", return_value=gate), \
         patch("src.services.slack.listeners.post_work_order_card", new_callable=AsyncMock):
        import asyncio
        ok = asyncio.run(sweep._post_due_linkedin_card(_order_with_created(old)))
    assert ok is False
    sql = _sql_of(session)
    assert "status = 'SKIPPED'" in sql
    assert "due_at = :next_due" not in sql
    session.commit.assert_called_once()


def json_payloads(session):
    """Concatenate the JSON payload bind params passed to session.execute."""
    import json as _json
    out = []
    for p in _params_of(session):
        val = p.get("payload") if isinstance(p, dict) else None
        if isinstance(val, str):
            out.append(val)
    return " ".join(out)


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
