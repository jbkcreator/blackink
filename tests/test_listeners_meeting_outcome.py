"""Unit tests for the meeting-outcome Slack modal entry point
(open_meeting_outcome_modal) and its view_submission handler
(handle_meeting_outcome_submit) in src.services.slack.listeners.

handle_meeting_outcome_submit contains the one cross-tenant DB read on this
branch (resolving which client_id owns a Slack-submitted contact_id) before
writing through the tenant-scoped record_outcome() path — the most
security-relevant code added here, and previously untested.

Follows tests/test_slack_listeners.py's pattern: call the plain async
functions directly with AsyncMock stand-ins for Bolt's injected args, no
live DB, no live Slack.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.slack import listeners


def _submit_view(contact_id="42", meeting_occurred_at="2026-09-05T14:00:00+00:00", attendance="Held") -> dict:
	return {
		"private_metadata": json.dumps({"contact_id": contact_id, "meeting_occurred_at": meeting_occurred_at}),
		"state": {
			"values": {
				"attendance_block": {"attendance": {"selected_option": {"value": attendance}}},
				"pm_software_block": {"pm_software": {"selected_option": None}},
				"door_count_block": {"door_count": {"value": None}},
				"objections_block": {"objections": {"selected_options": None}},
				"next_action_block": {"next_action": {"value": "Send proposal"}},
			}
		},
	}


# ── open_meeting_outcome_modal ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_open_meeting_outcome_modal_valid_inputs_opens_modal(monkeypatch):
	open_modal_mock = AsyncMock(return_value=True)
	monkeypatch.setattr(listeners.post, "open_modal", open_modal_mock)

	result = await listeners.open_meeting_outcome_modal(
		trigger_id="T1", contact_id="42", meeting_occurred_at="2026-09-05T14:00:00+00:00"
	)

	assert result is True
	open_modal_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_open_meeting_outcome_modal_invalid_datetime_returns_false(monkeypatch):
	open_modal_mock = AsyncMock(return_value=True)
	monkeypatch.setattr(listeners.post, "open_modal", open_modal_mock)

	result = await listeners.open_meeting_outcome_modal(
		trigger_id="T1", contact_id="42", meeting_occurred_at="not-a-date"
	)

	assert result is False
	open_modal_mock.assert_not_called()


@pytest.mark.asyncio
async def test_open_meeting_outcome_modal_invalid_contact_id_returns_false(monkeypatch):
	"""Finding #7: contact_id gets the same fail-fast validation as
	meeting_occurred_at, since it is also int()-cast unguarded at submit
	time."""
	open_modal_mock = AsyncMock(return_value=True)
	monkeypatch.setattr(listeners.post, "open_modal", open_modal_mock)

	result = await listeners.open_meeting_outcome_modal(
		trigger_id="T1", contact_id="not-an-int", meeting_occurred_at="2026-09-05T14:00:00+00:00"
	)

	assert result is False
	open_modal_mock.assert_not_called()


# ── handle_meeting_outcome_submit ────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_resolves_contact_to_client_and_records_outcome(monkeypatch):
	session = MagicMock()
	session.__enter__.return_value = session
	session.__exit__.return_value = False
	session.execute.return_value.mappings.return_value.first.return_value = {"owning_client_id": "acme_pm"}

	from contextlib import contextmanager

	@contextmanager
	def _fake_system_db():
		yield session

	monkeypatch.setattr(listeners, "get_system_db_context", _fake_system_db)
	record_mock = MagicMock()
	monkeypatch.setattr(listeners, "record_outcome", record_mock)
	notice_mock = AsyncMock()
	monkeypatch.setattr(listeners.post, "post_notice", notice_mock)

	ack = AsyncMock()
	body = {"user": {"id": "U1"}}
	await listeners.handle_meeting_outcome_submit(ack, body, _submit_view())

	ack.assert_awaited_once()
	record_mock.assert_called_once()
	assert record_mock.call_args.args[0] == "acme_pm"
	assert record_mock.call_args.kwargs["contact_id"] == 42
	notice_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_unresolvable_contact_posts_qa_notice_and_skips_record_outcome(monkeypatch):
	session = MagicMock()
	session.__enter__.return_value = session
	session.__exit__.return_value = False
	session.execute.return_value.mappings.return_value.first.return_value = None

	from contextlib import contextmanager

	@contextmanager
	def _fake_system_db():
		yield session

	monkeypatch.setattr(listeners, "get_system_db_context", _fake_system_db)
	record_mock = MagicMock()
	monkeypatch.setattr(listeners, "record_outcome", record_mock)
	notice_mock = AsyncMock()
	monkeypatch.setattr(listeners.post, "post_notice", notice_mock)

	ack = AsyncMock()
	body = {"user": {"id": "U1"}}
	await listeners.handle_meeting_outcome_submit(ack, body, _submit_view())

	record_mock.assert_not_called()
	notice_mock.assert_awaited_once()
	assert notice_mock.call_args.kwargs["channel_key"] == "qa"


@pytest.mark.asyncio
async def test_submit_null_owning_client_id_posts_qa_notice_and_skips_record_outcome(monkeypatch):
	session = MagicMock()
	session.__enter__.return_value = session
	session.__exit__.return_value = False
	session.execute.return_value.mappings.return_value.first.return_value = {"owning_client_id": None}

	from contextlib import contextmanager

	@contextmanager
	def _fake_system_db():
		yield session

	monkeypatch.setattr(listeners, "get_system_db_context", _fake_system_db)
	record_mock = MagicMock()
	monkeypatch.setattr(listeners, "record_outcome", record_mock)
	notice_mock = AsyncMock()
	monkeypatch.setattr(listeners.post, "post_notice", notice_mock)

	ack = AsyncMock()
	body = {"user": {"id": "U1"}}
	await listeners.handle_meeting_outcome_submit(ack, body, _submit_view())

	record_mock.assert_not_called()
	notice_mock.assert_awaited_once()
	assert notice_mock.call_args.kwargs["channel_key"] == "qa"


@pytest.mark.asyncio
async def test_submit_record_outcome_failure_posts_qa_notice_not_silently_lost(monkeypatch):
	"""Finding #4: record_outcome had no try/except — a raised exception
	after ack() would silently drop the rep's outcome capture with no
	notice anywhere. Must now post to qa, matching the unresolvable-contact
	branch's existing pattern."""
	session = MagicMock()
	session.__enter__.return_value = session
	session.__exit__.return_value = False
	session.execute.return_value.mappings.return_value.first.return_value = {"owning_client_id": "acme_pm"}

	from contextlib import contextmanager

	@contextmanager
	def _fake_system_db():
		yield session

	monkeypatch.setattr(listeners, "get_system_db_context", _fake_system_db)
	monkeypatch.setattr(listeners, "record_outcome", MagicMock(side_effect=RuntimeError("db unreachable")))
	notice_mock = AsyncMock()
	monkeypatch.setattr(listeners.post, "post_notice", notice_mock)

	ack = AsyncMock()
	body = {"user": {"id": "U1"}}
	await listeners.handle_meeting_outcome_submit(ack, body, _submit_view())

	ack.assert_awaited_once()
	notice_mock.assert_awaited_once()
	assert notice_mock.call_args.kwargs["channel_key"] == "qa"
	assert "42" in notice_mock.call_args.kwargs["text"]
