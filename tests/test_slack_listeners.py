"""Unit tests for src.services.slack.listeners — no live DB, no live Slack.

Calls the plain async functions directly (not through Bolt's dispatcher),
per the module's own design: every @app.* decorator is a thin wrapper
around a function that takes no Bolt-specific types, so it's testable with
AsyncMock injected for `respond`/`client`/`ack`.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.services.slack import listeners
from src.services.work_orders import WorkOrder


def _order(**overrides) -> WorkOrder:
	now = datetime.now(timezone.utc)
	base = dict(
		action_id="11111111-1111-1111-1111-111111111111",
		client_id="acme_pm",
		entity_type="contact",
		entity_id="contact-1",
		opportunity_id=None,
		agent_id="CAMPAIGN_AGENT",
		action_class="DISPATCH_EMAIL_TOUCH",
		autonomy_band="BAND_2_ONE_TAP",
		risk_class="LOW",
		confidence_score=None,
		recipient="a@b.com",
		payload={"subject": "Hi", "body": "Hello"},
		config_fingerprint={"channel": "email"},
		payload_hash="",
		hash_version=1,
		status="QUEUED",
		idempotency_key="idem-1",
		slack_channel_id="C123",
		slack_message_ts="1234.5678",
		decided_by=None,
		decided_at=None,
		execution_receipt=None,
		error=None,
		due_at=None,
		created_at=now,
		updated_at=now,
	)
	base.update(overrides)
	order = WorkOrder(**base)
	from src.services.slack import payload_hash as ph

	return WorkOrder(**{**base, "payload_hash": ph.compute(order)})


def _button_value(order: WorkOrder, decision: str = "APPROVED") -> dict:
	from src.services.slack import payload_hash as ph

	return json.loads(ph.button_value(order, decision))


# ── _snooze_until ─────────────────────────────────────────────────────────


def test_snooze_1h():
	now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
	assert listeners._snooze_until("1h", now) == now + timedelta(hours=1)


def test_snooze_4h():
	now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
	assert listeners._snooze_until("4h", now) == now + timedelta(hours=4)


def test_snooze_tomorrow_9am_is_9am_eastern_next_day():
	now = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)  # noon UTC = 8am EDT
	result = listeners._snooze_until("tomorrow_9am", now)
	from zoneinfo import ZoneInfo

	local = result.astimezone(ZoneInfo("America/New_York"))
	assert local.hour == 9 and local.minute == 0
	assert local.date() == now.astimezone(ZoneInfo("America/New_York")).date() + timedelta(days=1)


def test_snooze_unknown_key_raises():
	with pytest.raises(ValueError):
		listeners._snooze_until("next_tuesday")


# ── _load_and_verify ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_load_and_verify_not_authorized(monkeypatch):
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: False)
	respond = AsyncMock()
	with pytest.raises(listeners.ClickRejected):
		await listeners._load_and_verify({"client_id": "acme_pm", "action_id": "x"}, "U1", respond=respond)
	respond.assert_awaited_once()
	assert "Not authorized" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_load_and_verify_not_found(monkeypatch):
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: None)
	respond = AsyncMock()
	with pytest.raises(listeners.ClickRejected):
		await listeners._load_and_verify({"client_id": "acme_pm", "action_id": "missing"}, "U1", respond=respond)
	assert "could not be found" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_load_and_verify_stale_hash_reposts_fresh_card(monkeypatch):
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners, "_log_event", lambda *a, **k: None)
	repost_mock = AsyncMock()
	monkeypatch.setattr(listeners, "post_work_order_card", repost_mock)
	respond = AsyncMock()

	value = {"client_id": order.client_id, "action_id": order.action_id, "payload_hash": "0" * 64}
	with pytest.raises(listeners.ClickRejected):
		await listeners._load_and_verify(value, "U1", respond=respond)

	assert "out of date" in respond.call_args.kwargs["text"]
	repost_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_load_and_verify_integrity_failure_alerts_qa_but_does_not_block_on_it_alone(monkeypatch):
	"""A rogue stored-column mismatch alerts qa, but freshness is judged
	independently — if the button hash happens to match the (wrong)
	recomputed digest, integrity failure alone must not silently pass a
	stale click through as fresh. Here we simulate stored-column drift
	with a hash that IS fresh (matches current content) to isolate the
	integrity path from the freshness path."""
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners, "_log_event", lambda *a, **k: None)
	qa_mock = AsyncMock()
	monkeypatch.setattr(listeners.post, "post_notice", qa_mock)
	respond = AsyncMock()

	from src.services.slack import payload_hash as ph

	fresh_digest = ph.compute(order)
	drifted_order = WorkOrder(**{**order.__dict__, "payload_hash": "0" * 64})
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: drifted_order)

	value = {"client_id": order.client_id, "action_id": order.action_id, "payload_hash": fresh_digest}
	result = await listeners._load_and_verify(value, "U1", respond=respond)

	qa_mock.assert_awaited_once()
	assert result.action_id == order.action_id  # fresh click still proceeds


@pytest.mark.asyncio
async def test_load_and_verify_already_decided(monkeypatch):
	order = _order(status="APPROVED")
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	respond = AsyncMock()
	value = {"client_id": order.client_id, "action_id": order.action_id, "payload_hash": order.payload_hash}
	with pytest.raises(listeners.ClickRejected):
		await listeners._load_and_verify(value, "U1", respond=respond)
	assert "Already decided" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_load_and_verify_success_returns_order(monkeypatch):
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	respond = AsyncMock()
	value = {"client_id": order.client_id, "action_id": order.action_id, "payload_hash": order.payload_hash}
	result = await listeners._load_and_verify(value, "U1", respond=respond)
	assert result.action_id == order.action_id
	respond.assert_not_awaited()


# ── handle_terminal_action (approve/reject/skip/mark_done) ──────────────


@pytest.mark.asyncio
async def test_handle_terminal_action_happy_path(monkeypatch):
	order = _order()
	decided = WorkOrder(**{**order.__dict__, "status": "APPROVED", "decided_by": "slack:U1"})
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners.wo, "record_decision", lambda *a, **k: decided)
	monkeypatch.setattr(listeners, "_log_event", lambda *a, **k: None)
	update_mock = AsyncMock(return_value=True)
	monkeypatch.setattr(listeners.post, "update_card", update_mock)

	ack = AsyncMock()
	respond = AsyncMock()
	body = {"user": {"id": "U1"}}
	action = {"action_id": "approve", "value": json.dumps(_button_value(order, "APPROVED"))}

	await listeners.handle_terminal_action(ack, body, respond, action)

	ack.assert_awaited_once()
	update_mock.assert_awaited_once()
	respond.assert_not_awaited()  # success path never calls respond


@pytest.mark.asyncio
async def test_handle_terminal_action_malformed_value(monkeypatch):
	ack = AsyncMock()
	respond = AsyncMock()
	body = {"user": {"id": "U1"}}
	action = {"action_id": "approve", "value": "{not json"}
	await listeners.handle_terminal_action(ack, body, respond, action)
	ack.assert_awaited_once()
	respond.assert_awaited_once()
	assert "Malformed" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_handle_terminal_action_double_tap_race(monkeypatch):
	"""record_decision returning None (lost the race to a concurrent
	decision) must produce an informational response, not a crash."""
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners.wo, "record_decision", lambda *a, **k: None)

	ack = AsyncMock()
	respond = AsyncMock()
	body = {"user": {"id": "U1"}}
	action = {"action_id": "approve", "value": json.dumps(_button_value(order, "APPROVED"))}
	await listeners.handle_terminal_action(ack, body, respond, action)

	respond.assert_awaited_once()
	assert "just decided by someone else" in respond.call_args.kwargs["text"]


# ── handle_snooze — three buttons (1h/4h/tomorrow_9am), not a
# static_select. Confirmed live against the real Slack API that a select
# option's value has a 150-char limit a UUID + SHA-256 digest alone
# exceed; buttons don't share that limit. See _card_button_blocks. ──────


@pytest.mark.asyncio
async def test_handle_snooze_1h_button_happy_path(monkeypatch):
	order = _order()
	until = datetime.now(timezone.utc) + timedelta(hours=1)
	snoozed = WorkOrder(**{**order.__dict__, "status": "SNOOZED", "due_at": until})
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners.wo, "snooze", lambda *a, **k: snoozed)
	monkeypatch.setattr(listeners, "_log_event", lambda *a, **k: None)
	update_mock = AsyncMock(return_value=True)
	monkeypatch.setattr(listeners.post, "update_card", update_mock)

	value = _button_value(order, "SNOOZED")
	value["duration"] = "1h"
	ack = AsyncMock()
	respond = AsyncMock()
	body = {"user": {"id": "U1"}}
	action = {"action_id": "snooze_1h", "value": json.dumps(value)}

	await listeners.handle_snooze(ack, body, respond, action)

	ack.assert_awaited_once()
	update_mock.assert_awaited_once()
	respond.assert_not_awaited()


@pytest.mark.asyncio
async def test_handle_snooze_malformed_value(monkeypatch):
	ack = AsyncMock()
	respond = AsyncMock()
	body = {"user": {"id": "U1"}}
	action = {"action_id": "snooze_4h", "value": "{not json"}
	await listeners.handle_snooze(ack, body, respond, action)
	assert "Malformed" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_handle_snooze_double_tap_race(monkeypatch):
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners.wo, "snooze", lambda *a, **k: None)

	value = _button_value(order, "SNOOZED")
	value["duration"] = "tomorrow_9am"
	ack = AsyncMock()
	respond = AsyncMock()
	body = {"user": {"id": "U1"}}
	action = {"action_id": "snooze_tomorrow_9am", "value": json.dumps(value)}
	await listeners.handle_snooze(ack, body, respond, action)

	assert "just decided by someone else" in respond.call_args.kwargs["text"]


# ── halt / resume ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_halt_command_not_authorized(monkeypatch):
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: False)
	ack = AsyncMock()
	respond = AsyncMock()
	command = {"user_id": "U1", "text": "global test reason"}
	await listeners.handle_halt_command(ack, respond, command)
	assert "Not authorized" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_halt_command_global_issues_halt_and_posts_resume_button(monkeypatch):
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	issue_mock = lambda scope, *, scope_id, reason, issued_by: 7  # noqa: E731
	monkeypatch.setattr(listeners.halt_service, "issue_halt", issue_mock)
	monkeypatch.setattr(listeners, "generate_resume_token", lambda halt_id: "tok123")
	post_card_mock = AsyncMock()
	monkeypatch.setattr(listeners.post, "post_action_card", post_card_mock)

	ack = AsyncMock()
	respond = AsyncMock()
	command = {"user_id": "U1", "text": "global testing"}
	await listeners.handle_halt_command(ack, respond, command)

	post_card_mock.assert_awaited_once()
	assert post_card_mock.call_args.kwargs["channel_key"] == "command"


@pytest.mark.asyncio
async def test_halt_command_client_requires_scope_id(monkeypatch):
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	ack = AsyncMock()
	respond = AsyncMock()
	command = {"user_id": "U1", "text": "client"}
	await listeners.handle_halt_command(ack, respond, command)
	assert "Usage" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_halt_command_status_lists_active_halts(monkeypatch):
	from src.agents.relay.halt_state import HaltRecord

	monkeypatch.setattr(
		listeners.halt_service,
		"get_active_halts",
		lambda: [HaltRecord(halt_id=1, scope="GLOBAL", scope_id=None, reason="test", issued_by="slack:U1", issued_at=datetime.now(timezone.utc))],
	)
	ack = AsyncMock()
	respond = AsyncMock()
	await listeners.handle_halt_command(ack, respond, {"user_id": "U1", "text": "status"})
	assert "#1 GLOBAL" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_halt_resume_click_success(monkeypatch):
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.halt_service, "resume_halt", lambda halt_id, *, token, resumed_by: True)
	ack = AsyncMock()
	respond = AsyncMock()
	body = {"user": {"id": "U1"}}
	action = {"value": json.dumps({"halt_id": 7, "token": "tok123"})}
	await listeners.handle_halt_resume_click(ack, body, respond, action)
	assert "resumed" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_halt_resume_click_bad_token(monkeypatch):
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.halt_service, "resume_halt", lambda halt_id, *, token, resumed_by: False)
	ack = AsyncMock()
	respond = AsyncMock()
	body = {"user": {"id": "U1"}}
	action = {"value": json.dumps({"halt_id": 7, "token": "wrong"})}
	await listeners.handle_halt_resume_click(ack, body, respond, action)
	assert "Could not resume" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_halt_resume_click_not_authorized(monkeypatch):
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: False)
	resume_mock = AsyncMock()
	monkeypatch.setattr(listeners.halt_service, "resume_halt", resume_mock)
	ack = AsyncMock()
	respond = AsyncMock()
	body = {"user": {"id": "U1"}}
	action = {"value": json.dumps({"halt_id": 7, "token": "tok123"})}
	await listeners.handle_halt_resume_click(ack, body, respond, action)
	resume_mock.assert_not_called()
	assert "Not authorized" in respond.call_args.kwargs["text"]


# ── Revise authorization — PR #4 review finding 1. Both handlers used to
# hand-roll their own wo.get() load, skipping approver_authorized()
# entirely: any workspace member who could SEE a card could open the modal
# and submit it, forcing the order to SKIPPED. Revise now goes through the
# same _load_and_verify prelude as every terminal action, and the
# view_submission re-checks independently (it is a separate request). ────


def _revise_meta(order) -> str:
	from src.services.slack import payload_hash as ph

	return json.dumps(
		{
			"client_id": order.client_id,
			"action_id": order.action_id,
			"payload_hash": ph.compute(order),
			"channel_id": order.slack_channel_id,
			"message_ts": order.slack_message_ts,
		}
	)


def _revise_view(order, note: str = "please tighten the subject line") -> dict:
	return {
		"private_metadata": _revise_meta(order),
		"state": {"values": {"revision_note_block": {"revision_note": {"value": note}}}},
	}


@pytest.mark.asyncio
async def test_revise_open_rejects_unauthorized_user(monkeypatch):
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: False)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)

	ack, respond, client = AsyncMock(), AsyncMock(), AsyncMock()
	body = {"user": {"id": "U_INTRUDER"}, "trigger_id": "T1"}
	action = {"action_id": "revise", "value": json.dumps(_button_value(order, "REVISE"))}

	await listeners.handle_revise_open(ack, body, respond, action, client)

	client.views_open.assert_not_called()  # the modal never opens
	assert "Not authorized" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_revise_open_authorized_user_gets_the_modal(monkeypatch):
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)

	ack, respond, client = AsyncMock(), AsyncMock(), AsyncMock()
	body = {"user": {"id": "U1"}, "trigger_id": "T1"}
	action = {"action_id": "revise", "value": json.dumps(_button_value(order, "REVISE"))}

	await listeners.handle_revise_open(ack, body, respond, action, client)

	client.views_open.assert_awaited_once()
	assert client.views_open.call_args.kwargs["view"]["callback_id"] == listeners._REVISE_CALLBACK_ID


@pytest.mark.asyncio
async def test_revise_open_rejects_stale_card(monkeypatch):
	"""Revise now inherits the freshness check too — it previously had none
	at all, so a stale card could open a modal against rotated content."""
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners, "_log_event", lambda *a, **k: None)
	monkeypatch.setattr(listeners, "post_work_order_card", AsyncMock())

	ack, respond, client = AsyncMock(), AsyncMock(), AsyncMock()
	body = {"user": {"id": "U1"}, "trigger_id": "T1"}
	value = {"client_id": order.client_id, "action_id": order.action_id, "payload_hash": "0" * 64}
	await listeners.handle_revise_open(ack, body, respond, {"action_id": "revise", "value": json.dumps(value)}, client)

	client.views_open.assert_not_called()
	assert "out of date" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_revise_submit_rejects_unauthorized_user_and_does_not_skip_the_order(monkeypatch):
	"""The core of the finding: an unauthorized submit must not transition
	the order to SKIPPED. Asserted against the DB seam itself, so it fails
	if the guard is ever removed regardless of what the handler responds."""
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: False)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)

	db_mock = AsyncMock()
	monkeypatch.setattr(listeners, "get_db_context", db_mock)
	notice_mock = AsyncMock()
	monkeypatch.setattr(listeners.post, "post_notice", notice_mock)

	ack = AsyncMock()
	body = {"user": {"id": "U_INTRUDER"}}
	await listeners.handle_revise_submit(ack, body, _revise_view(order))

	db_mock.assert_not_called()  # no UPDATE ... SET status = 'SKIPPED'
	notice_mock.assert_not_awaited()
	assert ack.call_args.kwargs["response_action"] == "errors"
	assert "Not authorized" in ack.call_args.kwargs["errors"]["revision_note_block"]


@pytest.mark.asyncio
async def test_revise_submit_authorized_user_records_the_revision(monkeypatch):
	"""The guard must not break the legitimate path."""
	order = _order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners, "_log_event", lambda *a, **k: None)

	executed = []

	class _FakeSession:
		def execute(self, stmt, params=None):
			executed.append((str(stmt), params))

	from contextlib import contextmanager

	@contextmanager
	def _fake_db(client_id=None):
		yield _FakeSession()

	monkeypatch.setattr(listeners, "get_db_context", _fake_db)
	monkeypatch.setattr(listeners.post, "update_card", AsyncMock())
	monkeypatch.setattr(listeners.post, "post_notice", AsyncMock())

	ack = AsyncMock()
	await listeners.handle_revise_submit(ack, {"user": {"id": "U1"}}, _revise_view(order))

	assert len(executed) == 1
	sql, params = executed[0]
	assert "SKIPPED" in sql
	assert params["decided_by"] == "slack:U1"


@pytest.mark.asyncio
async def test_revise_submit_still_rejects_an_already_decided_order(monkeypatch):
	order = _order(status="APPROVED")
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	db_mock = AsyncMock()
	monkeypatch.setattr(listeners, "get_db_context", db_mock)

	ack = AsyncMock()
	await listeners.handle_revise_submit(ack, {"user": {"id": "U1"}}, _revise_view(order))

	db_mock.assert_not_called()
	assert "Already decided" in ack.call_args.kwargs["errors"]["revision_note_block"]


# ── handle_log_meeting_outcome (Addendum to Subtask 3.2.1) ──────────────


def _outcome_button_value(order: WorkOrder, *, contact_id="42",
                          meeting_occurred_at="2026-09-04T15:00:00+00:00") -> dict:
	from src.services.slack import payload_hash as ph
	return {
		"client_id": order.client_id,
		"action_id": order.action_id,
		"contact_id": contact_id,
		"meeting_occurred_at": meeting_occurred_at,
		"payload_hash": ph.compute(order),
	}


def _outcome_order(**overrides) -> WorkOrder:
	base = dict(
		action_class="MEETING_OUTCOME_PROMPT",
		recipient="U1",
		payload={"booking_id": 7, "company_id": "co-7", "contact_id": 42, "meeting_occurred_at": "2026-09-04T15:00:00+00:00"},
	)
	base.update(overrides)
	return _order(**base)


@pytest.mark.asyncio
async def test_log_outcome_opens_modal_for_assigned_closer(monkeypatch):
	order = _outcome_order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	ack, respond, client = AsyncMock(), AsyncMock(), AsyncMock()
	body = {"user": {"id": "U1"}, "trigger_id": "trg-1"}
	action = {"action_id": "log_meeting_outcome", "value": json.dumps(_outcome_button_value(order))}

	await listeners.handle_log_meeting_outcome(ack, body, respond, action, client)

	ack.assert_awaited_once()
	client.views_open.assert_awaited_once()
	respond.assert_not_awaited()


@pytest.mark.asyncio
async def test_log_outcome_rejects_different_closer(monkeypatch):
	order = _outcome_order(recipient="U_OTHER")
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	ack, respond, client = AsyncMock(), AsyncMock(), AsyncMock()
	body = {"user": {"id": "U1"}, "trigger_id": "trg-1"}
	action = {"action_id": "log_meeting_outcome", "value": json.dumps(_outcome_button_value(order))}

	await listeners.handle_log_meeting_outcome(ack, body, respond, action, client)

	client.views_open.assert_not_called()
	assert "different closer" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_log_outcome_rejects_expired_card(monkeypatch):
	# Fresh hash, but posted more than 24h ago -> expired, same rejection as altered.
	old = datetime.now(timezone.utc) - timedelta(hours=24, minutes=1)
	order = _outcome_order(created_at=old, updated_at=old)
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners, "_log_event", lambda *a, **k: None)
	ack, respond, client = AsyncMock(), AsyncMock(), AsyncMock()
	body = {"user": {"id": "U1"}, "trigger_id": "trg-1"}
	action = {"action_id": "log_meeting_outcome", "value": json.dumps(_outcome_button_value(order))}

	await listeners.handle_log_meeting_outcome(ack, body, respond, action, client)

	client.views_open.assert_not_called()
	assert "expired or was altered" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_log_outcome_rejects_altered_hash(monkeypatch):
	order = _outcome_order()
	monkeypatch.setattr(listeners, "approver_authorized", lambda user_id, client_id=None: True)
	monkeypatch.setattr(listeners.wo, "get", lambda client_id, action_id: order)
	monkeypatch.setattr(listeners, "_log_event", lambda *a, **k: None)
	ack, respond, client = AsyncMock(), AsyncMock(), AsyncMock()
	value = _outcome_button_value(order)
	value["payload_hash"] = "deadbeef" * 8  # wrong, but well-formed length
	body = {"user": {"id": "U1"}, "trigger_id": "trg-1"}
	action = {"action_id": "log_meeting_outcome", "value": json.dumps(value)}

	await listeners.handle_log_meeting_outcome(ack, body, respond, action, client)

	client.views_open.assert_not_called()
	assert "expired or was altered" in respond.call_args.kwargs["text"]


@pytest.mark.asyncio
async def test_log_outcome_malformed_value(monkeypatch):
	ack, respond, client = AsyncMock(), AsyncMock(), AsyncMock()
	body = {"user": {"id": "U1"}, "trigger_id": "trg-1"}
	action = {"action_id": "log_meeting_outcome", "value": "{not json"}
	await listeners.handle_log_meeting_outcome(ack, body, respond, action, client)
	ack.assert_awaited_once()
	assert "Malformed" in respond.call_args.kwargs["text"]
	client.views_open.assert_not_called()
