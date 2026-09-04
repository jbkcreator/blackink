"""Tests for the post-booking "Log Outcome" trigger card (Addendum to
Subtask 3.2.1).

Pure-unit tests (card rendering, the modal's guard clauses) need no DB.
The claim/self-heal/post/sweep tests need real Postgres for the same
reason as tests/test_no_show_prompts.py — the SKIP LOCKED claim and the
as_of-parameterized windows can't be faithfully emulated by a FakeSession.

Slack is unconfigured in the test environment, so post.post_action_card /
post.post_notice return None; the happy-path and ping tests patch them
with AsyncMocks. The work order the card is built on is a REAL
agent_work_orders row (no mock) so the recipient binding and button hash
are exercised end to end.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context
from src.services import work_orders as wo
from src.services.booking_ingest import schedule_meeting_outcome_prompt
from src.services.meeting_outcome_prompts import (
	CARD_TTL,
	UNCLICKED_REMINDER_AFTER,
	card_blocks,
	claim_prompts,
	post_prompt,
	self_heal_blocked,
	sweep_posted_cards,
)
from src.services.slack import payload_hash

_BLACKINK_INTERNAL_SALES = "BLACKINK_INTERNAL_SALES"
_REP = "U_REP_PRIMARY"


# ── Pure-unit: card rendering ──────────────────────────────────────────

def test_card_blocks_includes_name_company_and_rep_mention():
	blocks = card_blocks(
		prospect_name="Dana Prospect",
		company_name="Prospect PM LLC",
		scheduled_at=datetime(2026, 9, 4, 15, 0, tzinfo=timezone.utc),
		rep_slack_user_id=_REP,
		button_value="{}",
	)
	text_blob = json.dumps(blocks)
	assert "Dana Prospect" in text_blob
	assert "Prospect PM LLC" in text_blob
	assert f"<@{_REP}>" in text_blob
	# one actionable button, the id the click handler is registered on
	button = blocks[1]["elements"][0]
	assert button["action_id"] == "log_meeting_outcome"


# ── Pure-unit: modal guard clauses ─────────────────────────────────────

def _import_modal():
	# imported lazily so the module's Bolt-app import doesn't run at collection
	from src.services.slack.listeners import open_meeting_outcome_modal
	return open_meeting_outcome_modal


def test_open_modal_rejects_non_int_contact_id():
	import asyncio
	open_modal = _import_modal()
	client = AsyncMock()
	opened = asyncio.run(open_modal(
		client, trigger_id="t", contact_id="not-an-int",
		meeting_occurred_at="2026-09-04T15:00:00+00:00", private_metadata="{}",
	))
	assert opened is False
	client.views_open.assert_not_called()


def test_open_modal_rejects_non_iso_meeting_time():
	import asyncio
	open_modal = _import_modal()
	client = AsyncMock()
	opened = asyncio.run(open_modal(
		client, trigger_id="t", contact_id="42",
		meeting_occurred_at="yesterday afternoon", private_metadata="{}",
	))
	assert opened is False
	client.views_open.assert_not_called()


def test_open_modal_opens_for_valid_inputs():
	import asyncio
	open_modal = _import_modal()
	client = AsyncMock()
	opened = asyncio.run(open_modal(
		client, trigger_id="t", contact_id="42",
		meeting_occurred_at="2026-09-04T15:00:00+00:00", private_metadata="{}",
	))
	assert opened is True
	client.views_open.assert_awaited_once()


# ── Live-DB fixture ────────────────────────────────────────────────────

@pytest.fixture
def sales_demo():
	"""One INTERNAL_SALES_DEMO connection (rep provisioned) + one matched,
	CONFIRMED booking + one PENDING meeting_outcome_prompt_jobs row."""
	with get_owner_db_context() as session:
		session.execute(text(
			"DELETE FROM meeting_outcome_prompt_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
		session.execute(text(
			"DELETE FROM no_show_prompt_jobs WHERE booking_id IN "
			"(SELECT booking_id FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES')"
		))
		session.execute(text("DELETE FROM meeting_outcomes WHERE contact_id IN "
			"(SELECT contact_id FROM contacts WHERE email = 'outcome-target@example.com')"))
		session.execute(text("DELETE FROM agent_work_orders WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
	with get_system_db_context() as session:
		session.execute(text("DELETE FROM bookings WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
		session.execute(text("DELETE FROM calendar_connections WHERE client_id = 'BLACKINK_INTERNAL_SALES'"))
	with get_owner_db_context() as session:
		session.execute(text("DELETE FROM contacts WHERE email = 'outcome-target@example.com'"))
		session.execute(text("DELETE FROM companies WHERE company_id = 'test-outcome-co'"))
		company = session.execute(text(
			"INSERT INTO companies (company_id, company_name, domain, county_slug, status) "
			"VALUES ('test-outcome-co', 'Test Outcome PM Co', 'outcome-test.example.com', "
			"(SELECT county_slug FROM counties LIMIT 1), 'PROSPECTING') RETURNING company_id"
		)).one()
		contact = session.execute(text(
			"INSERT INTO contacts (company_id, contact_role_type, first_name, last_name, email, phone) "
			"VALUES (:cid, 'OWNER_BROKER_MD', 'Dana', 'Prospect', 'outcome-target@example.com', '+14075559876') "
			"RETURNING contact_id"
		), {"cid": company.company_id}).one()

	with get_system_db_context() as session:
		conn = session.execute(text(
			"INSERT INTO calendar_connections "
			"(client_id, provider, connection_scope, external_calendar_id, subscription_id, "
			" verification_secret, initial_sync_done, rep_slack_user_id) "
			"VALUES (:cid, 'GOOGLE', 'INTERNAL_SALES_DEMO', 'rep-primary', 'sub-outcome', 'secret', TRUE, :rep) "
			"RETURNING connection_id"
		), {"cid": _BLACKINK_INTERNAL_SALES, "rep": _REP}).one()
		scheduled_at = datetime.now(timezone.utc) - timedelta(minutes=2)
		booking = session.execute(text(
			"INSERT INTO bookings (client_id, provider, calendar_connection_id, external_event_id, "
			"event_status, scheduled_at, raw_payload, target_company_id, target_contact_id, status) "
			"VALUES (:cid, 'GOOGLE', :conn, 'evt-outcome', 'CONFIRMED', :sched, '{}', "
			":company_id, :contact_id, 'MATCHED') RETURNING booking_id"
		), {
			"cid": _BLACKINK_INTERNAL_SALES, "conn": conn.connection_id, "sched": scheduled_at,
			"company_id": company.company_id, "contact_id": contact.contact_id,
		}).one()
		job = session.execute(text(
			"INSERT INTO meeting_outcome_prompt_jobs (client_id, booking_id, scheduled_for, status) "
			"VALUES (:cid, :bid, :sched, 'PENDING') RETURNING prompt_job_id"
		), {"cid": _BLACKINK_INTERNAL_SALES, "bid": booking.booking_id, "sched": scheduled_at}).one()

	yield {
		"booking_id": booking.booking_id, "prompt_job_id": job.prompt_job_id,
		"connection_id": conn.connection_id, "contact_id": contact.contact_id,
		"company_id": company.company_id, "scheduled_at": scheduled_at,
	}


def _job_status(prompt_job_id):
	with get_system_db_context() as session:
		return session.execute(text(
			"SELECT status, last_error, work_order_action_id, posted_at, reminder_sent_at "
			"FROM meeting_outcome_prompt_jobs WHERE prompt_job_id = :id"
		), {"id": prompt_job_id}).one()


# ── Live-DB: claim / block / self-heal ─────────────────────────────────

def test_claim_prompts_claims_due_pending_job(sales_demo):
	with get_system_db_context() as session:
		claimed = claim_prompts(session, claim_time=datetime.now(timezone.utc))
	assert sales_demo["prompt_job_id"] in claimed


def test_post_prompt_blocked_when_no_rep_slack_user_id(sales_demo):
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE calendar_connections SET rep_slack_user_id = NULL WHERE connection_id = :id"),
			{"id": sales_demo["connection_id"]},
		)
	import asyncio
	with get_system_db_context() as session:
		asyncio.run(post_prompt(session, sales_demo["prompt_job_id"], as_of=datetime.now(timezone.utc)))
	row = _job_status(sales_demo["prompt_job_id"])
	assert row.status == "BLOCKED"
	assert row.last_error == "MISSING_REP_SLACK_USER_ID"


def test_self_heal_promotes_once_rep_provisioned(sales_demo):
	# Drive the row to BLOCKED/MISSING_REP_SLACK_USER_ID first.
	import asyncio
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE calendar_connections SET rep_slack_user_id = NULL WHERE connection_id = :id"),
			{"id": sales_demo["connection_id"]},
		)
	with get_system_db_context() as session:
		asyncio.run(post_prompt(session, sales_demo["prompt_job_id"], as_of=datetime.now(timezone.utc)))
	assert _job_status(sales_demo["prompt_job_id"]).status == "BLOCKED"

	# Operator provisions the mapping; self-heal returns it to PENDING.
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE calendar_connections SET rep_slack_user_id = :rep WHERE connection_id = :id"),
			{"rep": _REP, "id": sales_demo["connection_id"]},
		)
	with get_system_db_context() as session:
		promoted = self_heal_blocked(session)
	assert promoted >= 1
	assert _job_status(sales_demo["prompt_job_id"]).status == "PENDING"


def test_post_prompt_blocked_when_target_unresolved(sales_demo):
	import asyncio
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE bookings SET target_contact_id = NULL WHERE booking_id = :id"),
			{"id": sales_demo["booking_id"]},
		)
	with get_system_db_context() as session:
		asyncio.run(post_prompt(session, sales_demo["prompt_job_id"], as_of=datetime.now(timezone.utc)))
	row = _job_status(sales_demo["prompt_job_id"])
	assert row.status == "BLOCKED"
	assert row.last_error == "UNRESOLVED_TARGET"


def test_post_prompt_skipped_when_outcome_already_recorded(sales_demo):
	import asyncio
	# An outcome logged from either surface (e.g. #blackink-command no-show)
	# means this card must not also post.
	with get_owner_db_context() as session:
		session.execute(text(
			"INSERT INTO meeting_outcomes (client_id, contact_id, company_id, meeting_occurred_at, attendance_status) "
			"VALUES (:cid, :contact, :company, :occurred, 'NO_SHOW')"
		), {
			"cid": _BLACKINK_INTERNAL_SALES, "contact": sales_demo["contact_id"],
			"company": sales_demo["company_id"], "occurred": sales_demo["scheduled_at"],
		})
	with get_system_db_context() as session:
		asyncio.run(post_prompt(session, sales_demo["prompt_job_id"], as_of=datetime.now(timezone.utc)))
	row = _job_status(sales_demo["prompt_job_id"])
	assert row.status == "SKIPPED"
	assert row.last_error == "OUTCOME_ALREADY_RECORDED"


def test_post_prompt_happy_path_creates_work_order_and_bound_button(sales_demo):
	import asyncio
	fake_post = AsyncMock(return_value={"channel_id": "C_SETTER", "message_ts": "1699.0001"})
	with patch("src.services.meeting_outcome_prompts.post.post_action_card", fake_post):
		with get_system_db_context() as session:
			asyncio.run(post_prompt(session, sales_demo["prompt_job_id"], as_of=datetime.now(timezone.utc)))

	row = _job_status(sales_demo["prompt_job_id"])
	assert row.status == "SENT"
	assert row.work_order_action_id is not None
	assert row.posted_at is not None

	order = wo.get(_BLACKINK_INTERNAL_SALES, str(row.work_order_action_id))
	assert order is not None
	assert order.recipient == _REP  # the assigned closer, bound into the hash
	assert order.action_class == "MEETING_OUTCOME_PROMPT"
	assert order.status == "QUEUED"

	# The button value carries contact_id as an int-parseable string and a
	# payload_hash that verifies fresh against the order it was built from.
	blocks = fake_post.call_args.kwargs["blocks"]
	value = json.loads(blocks[1]["elements"][0]["value"])
	assert value["contact_id"] == str(sales_demo["contact_id"])
	assert int(value["contact_id"]) == sales_demo["contact_id"]
	assert payload_hash.verify(order, value["payload_hash"]).fresh is True


# ── Live-DB: cancel via the scheduler ──────────────────────────────────

def test_schedule_cancel_marks_job_cancelled(sales_demo):
	with get_system_db_context() as session:
		schedule_meeting_outcome_prompt(
			session, client_id=_BLACKINK_INTERNAL_SALES, booking_id=sales_demo["booking_id"],
			old_event_status="CONFIRMED", old_scheduled_at=None,
			new_event_status="CANCELLED", new_scheduled_at=None,
		)
	assert _job_status(sales_demo["prompt_job_id"]).status == "CANCELLED"


# ── Live-DB: the 4h ping and 24h expiry ────────────────────────────────

def _make_sent_card(sales_demo, posted_at):
	"""Post the card (mocked Slack) so it reaches SENT with a real work
	order, then backdate posted_at to exercise the follow-up windows."""
	import asyncio
	with patch(
		"src.services.meeting_outcome_prompts.post.post_action_card",
		AsyncMock(return_value={"channel_id": "C_SETTER", "message_ts": "1699.0001"}),
	):
		with get_system_db_context() as session:
			asyncio.run(post_prompt(session, sales_demo["prompt_job_id"], as_of=datetime.now(timezone.utc)))
	with get_system_db_context() as session:
		session.execute(
			text("UPDATE meeting_outcome_prompt_jobs SET posted_at = :p WHERE prompt_job_id = :id"),
			{"p": posted_at, "id": sales_demo["prompt_job_id"]},
		)


def test_sweep_pings_unclicked_card_once_after_4h(sales_demo):
	import asyncio
	now = datetime.now(timezone.utc)
	_make_sent_card(sales_demo, posted_at=now - (UNCLICKED_REMINDER_AFTER + timedelta(minutes=1)))

	fake_notice = AsyncMock(return_value="1699.0002")
	with patch("src.services.meeting_outcome_prompts.post.post_notice", fake_notice):
		with get_system_db_context() as session:
			result = asyncio.run(sweep_posted_cards(session, as_of=now))
	assert result["pinged"] == 1
	# threaded under the card
	assert fake_notice.call_args.kwargs["thread_ts"] == "1699.0001"
	assert _job_status(sales_demo["prompt_job_id"]).reminder_sent_at is not None

	# A second sweep does not ping again (reminder_sent_at now set).
	with patch("src.services.meeting_outcome_prompts.post.post_notice", fake_notice):
		with get_system_db_context() as session:
			again = asyncio.run(sweep_posted_cards(session, as_of=now))
	assert again["pinged"] == 0


def test_sweep_expires_card_after_24h(sales_demo):
	import asyncio
	now = datetime.now(timezone.utc)
	_make_sent_card(sales_demo, posted_at=now - (CARD_TTL + timedelta(minutes=1)))
	with get_system_db_context() as session:
		result = asyncio.run(sweep_posted_cards(session, as_of=now))
	assert result["expired"] == 1
	assert _job_status(sales_demo["prompt_job_id"]).status == "EXPIRED"


# ── Live-DB: the modal submission (handle_meeting_outcome_submit) ───────

def _submit_view(order, *, private_meta, attendance="ATTENDED",
                 objections="Price too high", pm_software=None,
                 door_count=None, next_action=None):
	values = {
		"attendance_block": {"attendance_status": {"type": "static_select", "selected_option": {"value": attendance}}},
		"pm_software_block": {"pm_software_stated": {"type": "plain_text_input", "value": pm_software}},
		"door_count_block": {"door_count_stated": {"type": "plain_text_input", "value": door_count}},
		"objections_block": {"objections_stated": {"type": "plain_text_input", "value": objections}},
		"next_action_block": {"next_action": {"type": "plain_text_input", "value": next_action}},
	}
	return {"private_metadata": private_meta, "state": {"values": values}}


def _posted_order(sales_demo):
	"""Drive the fixture's job to SENT (mocked Slack) and return the real
	QUEUED work order behind the card."""
	import asyncio
	with patch(
		"src.services.meeting_outcome_prompts.post.post_action_card",
		AsyncMock(return_value={"channel_id": "C_SETTER", "message_ts": "1699.0001"}),
	):
		with get_system_db_context() as session:
			asyncio.run(post_prompt(session, sales_demo["prompt_job_id"], as_of=datetime.now(timezone.utc)))
	row = _job_status(sales_demo["prompt_job_id"])
	return wo.get(_BLACKINK_INTERNAL_SALES, str(row.work_order_action_id))


def test_submit_writes_outcome_and_mirrors_objections(sales_demo):
	"""The core DoD + the RLS-mirror fix: the submission writes a
	meeting_outcomes row AND updates contacts.prospect_objections, even
	though this runs under a BLACKINK_INTERNAL_SALES-scoped session whose
	prospect company has owning_client_id = NULL (a plain UPDATE would
	silently match zero rows — the SECURITY DEFINER mirror is what makes
	this pass)."""
	import asyncio
	from src.services.slack import listeners

	order = _posted_order(sales_demo)
	meta = json.dumps({
		"client_id": order.client_id, "action_id": order.action_id,
		"payload_hash": payload_hash.compute(order),
		"booking_id": sales_demo["booking_id"], "contact_id": sales_demo["contact_id"],
		"company_id": sales_demo["company_id"],
		"meeting_occurred_at": sales_demo["scheduled_at"].isoformat(),
		"channel_id": order.slack_channel_id, "message_ts": order.slack_message_ts,
	})
	view = _submit_view(order, private_meta=meta, objections="Price too high", pm_software="AppFolio", door_count="120")
	ack = AsyncMock()
	body = {"user": {"id": _REP}}

	with patch.object(listeners, "approver_authorized", lambda user_id, client_id=None: True), \
	     patch.object(listeners.post, "update_card", AsyncMock(return_value=True)), \
	     patch.object(listeners, "_log_event", lambda *a, **k: None):
		asyncio.run(listeners.handle_meeting_outcome_submit(ack, body, view))

	# ack() with no response_action == accepted, modal closes.
	assert ack.await_args is None or not ack.await_args.kwargs.get("response_action")

	with get_owner_db_context() as session:
		outcome = session.execute(text(
			"SELECT attendance_status, objections_stated FROM meeting_outcomes "
			"WHERE contact_id = :cid ORDER BY outcome_id DESC LIMIT 1"
		), {"cid": sales_demo["contact_id"]}).one()
		assert outcome.attendance_status == "ATTENDED"
		assert outcome.objections_stated == "Price too high"
		# The mirror actually landed on the RLS-scoped contacts row.
		objections = session.execute(text(
			"SELECT prospect_objections FROM contacts WHERE contact_id = :cid"
		), {"cid": sales_demo["contact_id"]}).scalar()
		assert objections == "Price too high"
		pm_software = session.execute(text(
			"SELECT current_pm_software FROM companies WHERE company_id = :cid"
		), {"cid": sales_demo["company_id"]}).scalar()
		assert pm_software == "AppFolio"

	# The work order is closed out.
	assert wo.get(_BLACKINK_INTERNAL_SALES, order.action_id).status == "DONE"


def test_submit_rejected_when_outcome_already_recorded(sales_demo):
	"""A NO_SHOW logged from the #blackink-command card first must block the
	setter-channel submission — the two surfaces log the same meeting."""
	import asyncio
	from src.services.slack import listeners

	order = _posted_order(sales_demo)
	with get_owner_db_context() as session:
		session.execute(text(
			"INSERT INTO meeting_outcomes (client_id, contact_id, company_id, meeting_occurred_at, attendance_status) "
			"VALUES (:cid, :contact, :company, :occurred, 'NO_SHOW')"
		), {
			"cid": _BLACKINK_INTERNAL_SALES, "contact": sales_demo["contact_id"],
			"company": sales_demo["company_id"], "occurred": sales_demo["scheduled_at"],
		})
	meta = json.dumps({
		"client_id": order.client_id, "action_id": order.action_id,
		"payload_hash": payload_hash.compute(order),
		"booking_id": sales_demo["booking_id"], "contact_id": sales_demo["contact_id"],
		"company_id": sales_demo["company_id"],
		"meeting_occurred_at": sales_demo["scheduled_at"].isoformat(),
		"channel_id": order.slack_channel_id, "message_ts": order.slack_message_ts,
	})
	view = _submit_view(order, private_meta=meta)
	ack = AsyncMock()
	body = {"user": {"id": _REP}}

	with patch.object(listeners, "approver_authorized", lambda user_id, client_id=None: True):
		asyncio.run(listeners.handle_meeting_outcome_submit(ack, body, view))

	assert ack.await_args.kwargs.get("response_action") == "errors"
	assert wo.get(_BLACKINK_INTERNAL_SALES, order.action_id).status == "QUEUED"
