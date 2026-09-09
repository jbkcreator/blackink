"""Tests for winback_sequencer.py — arming, priority ordering, the
compliance gate, stop handling, and per-touch dispatch (Subtask 3.1.2).
No live DB — mirrors tests/test_sequence_orchestrator.py's MagicMock/patch
style for dispatch_winback_touch, and a lightweight FakeSession for the
SQL-driven helpers."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.services.email_sender import SendResult
from src.services.winback_sequencer import (
	arm_winback_run,
	dispatch_winback_touch,
	evaluate_winback_touch_gate,
	stop_active_winback_runs,
)


def _row(**overrides):
	base = dict(
		winback_row_id=1,
		owner_name="Jane Doe",
		property_address_raw="1 Main St",
		property_address_normalized="1 MAIN ST",
		county_slug="hillsborough_fl",
		email="owner@example.com",
		disposition="STILL_OWNS_STILL_RENTING",
		suppression_state=False,
		suppression_reason=None,
		stopped_at=None,
		stop_reason=None,
		audit_loss_dollars_est=None,
		# Subtask 3.2.1 — default to "already enriched, healthy" so every
		# pre-existing test above this line (disposition/suppression/stopped_at
		# assertions) continues to exercise exactly what it did before the
		# enrichment gate landed, without having to know about it.
		enrichment_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
		requires_enrichment_review=False,
		email_status="VERIFIED",
	)
	base.update(overrides)
	return SimpleNamespace(**base)


# ---------------------------------------------------------------------------
# evaluate_winback_touch_gate
# ---------------------------------------------------------------------------

def test_gate_ready_for_armable_unsuppressed_unstopped_row():
	gate = evaluate_winback_touch_gate(MagicMock(), _row(), "client_a")
	assert gate.ready
	assert gate.blocked_reasons == ()


def test_gate_blocks_sold_disposition():
	gate = evaluate_winback_touch_gate(MagicMock(), _row(disposition="SOLD"), "client_a")
	assert not gate.ready
	assert any("disposition" in r for r in gate.blocked_reasons)


def test_gate_blocks_suppressed_row():
	gate = evaluate_winback_touch_gate(MagicMock(), _row(suppression_state=True, suppression_reason="DNC_LISTED"), "client_a")
	assert not gate.ready
	assert any("DNC_LISTED" in r for r in gate.blocked_reasons)


def test_gate_blocks_stopped_row():
	gate = evaluate_winback_touch_gate(
		MagicMock(), _row(stopped_at=datetime.now(timezone.utc), stop_reason="REPLY"), "client_a",
	)
	assert not gate.ready
	assert any("REPLY" in r for r in gate.blocked_reasons)


def test_gate_blocks_dnc_unverified_row():
	"""Confirmed review finding: a row whose DNC status couldn't be
	affirmatively verified must be blocked the same way a genuine DNC hit
	is — evaluate_winback_touch_gate's suppression check is reason-agnostic,
	so this closes the loop end to end (no work order can ever send for
	such a row, however it got created)."""
	gate = evaluate_winback_touch_gate(
		MagicMock(), _row(suppression_state=True, suppression_reason="DNC_UNVERIFIED"), "client_a",
	)
	assert not gate.ready
	assert any("DNC_UNVERIFIED" in r for r in gate.blocked_reasons)


def test_gate_blocks_missing_email():
	gate = evaluate_winback_touch_gate(MagicMock(), _row(email=None), "client_a")
	assert not gate.ready


class _GateAuditFakeSession:
	"""Records every INSERT INTO winback_gate_checks — audit finding #5."""

	def __init__(self):
		self.recorded: list[dict] = []

	def execute(self, stmt, params=None):
		if "INSERT INTO winback_gate_checks" in str(stmt):
			self.recorded.append(dict(params))
		return MagicMock()


def test_gate_records_every_check_to_the_audit_table():
	session = _GateAuditFakeSession()
	evaluate_winback_touch_gate(session, _row(winback_row_id=99), "client_a")
	assert len(session.recorded) == 4  # one row per check, always, not just failures
	assert {r["check_name"] for r in session.recorded} == {
		"disposition", "not_suppressed", "not_stopped", "enrichment_verified",
	}
	assert all(r["winback_row_id"] == 99 and r["client_id"] == "client_a" for r in session.recorded)
	assert all(r["status"] == "PASS" for r in session.recorded)


def test_gate_records_failing_checks_too_not_just_passes():
	session = _GateAuditFakeSession()
	evaluate_winback_touch_gate(session, _row(disposition="SOLD"), "client_a")
	disposition_row = next(r for r in session.recorded if r["check_name"] == "disposition")
	assert disposition_row["status"] == "FAIL"
	assert "SOLD" in disposition_row["detail"]
	# every OTHER check still gets recorded too — no short-circuit on first failure.
	assert len(session.recorded) == 4


# ---------------------------------------------------------------------------
# stop_active_winback_runs
# ---------------------------------------------------------------------------

def test_stop_active_winback_runs_updates_matching_rows():
	session = MagicMock()
	session.execute.return_value.rowcount = 1
	count = stop_active_winback_runs(session, "client_a", "owner@example.com", "OPT_OUT")
	assert count == 1
	_stmt, params = session.execute.call_args.args
	assert params == {"client_id": "client_a", "email": "owner@example.com", "reason": "OPT_OUT"}


def test_stop_active_winback_runs_rejects_unknown_reason():
	with pytest.raises(ValueError):
		stop_active_winback_runs(MagicMock(), "client_a", "owner@example.com", "BOGUS")


def test_stop_active_winback_runs_is_a_noop_when_nothing_matches():
	session = MagicMock()
	session.execute.return_value.rowcount = 0
	count = stop_active_winback_runs(session, "client_a", "nobody@example.com", "REPLY")
	assert count == 0


# ---------------------------------------------------------------------------
# arm_winback_run — priority ordering (the DoD's own mechanism)
# ---------------------------------------------------------------------------

class _ArmFakeSession:
	"""Handles the county_name lookup only — enqueue() itself is patched."""

	def execute(self, stmt, params=None):
		result = MagicMock()
		result.first.return_value = SimpleNamespace(county_name="Hillsborough")
		return result


def test_still_owns_still_renting_gets_zero_day_base_offset():
	armed_at = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
	enqueued_due_ats = []

	def _fake_enqueue(**kwargs):
		enqueued_due_ats.append(kwargs["due_at"])
		return SimpleNamespace(action_id=f"order-{kwargs['payload']['touch_step']}")

	with (
		patch("src.services.winback_sequencer.wo.enqueue", side_effect=_fake_enqueue),
		patch("src.services.winback_sequencer.resolve_owner_booking_link", return_value=None),
	):
		arm_winback_run(_ArmFakeSession(), "client_a", _row(disposition="STILL_OWNS_STILL_RENTING"), armed_at)

	assert enqueued_due_ats == [
		armed_at + timedelta(days=0),
		armed_at + timedelta(days=5),
		armed_at + timedelta(days=12),
	]


def test_still_owns_not_renting_gets_one_day_base_offset():
	armed_at = datetime(2026, 9, 15, 12, 0, tzinfo=timezone.utc)
	enqueued_due_ats = []

	def _fake_enqueue(**kwargs):
		enqueued_due_ats.append(kwargs["due_at"])
		return SimpleNamespace(action_id=f"order-{kwargs['payload']['touch_step']}")

	with (
		patch("src.services.winback_sequencer.wo.enqueue", side_effect=_fake_enqueue),
		patch("src.services.winback_sequencer.resolve_owner_booking_link", return_value=None),
	):
		arm_winback_run(_ArmFakeSession(), "client_a", _row(disposition="STILL_OWNS_NOT_RENTING"), armed_at)

	# +1 day base offset guarantees every STILL_OWNS_STILL_RENTING Touch 1
	# (due at armed_at + 0) was already queued before this bucket's Touch 1
	# (due at armed_at + 1 day) ever becomes due.
	assert enqueued_due_ats == [
		armed_at + timedelta(days=1),
		armed_at + timedelta(days=6),
		armed_at + timedelta(days=13),
	]


def test_arm_skips_non_armable_disposition():
	with patch("src.services.winback_sequencer.wo.enqueue") as mock_enqueue:
		action_ids = arm_winback_run(_ArmFakeSession(), "client_a", _row(disposition="SOLD"), datetime.now(timezone.utc))
	assert action_ids == []
	mock_enqueue.assert_not_called()


def test_arm_skips_suppressed_row_even_if_caller_did_not_pre_filter():
	"""Defense in depth (review finding): arm_winback_run must not rely
	solely on the /arm endpoint's own SQL filter."""
	with patch("src.services.winback_sequencer.wo.enqueue") as mock_enqueue:
		action_ids = arm_winback_run(
			_ArmFakeSession(), "client_a",
			_row(suppression_state=True, suppression_reason="DNC_UNVERIFIED"),
			datetime.now(timezone.utc),
		)
	assert action_ids == []
	mock_enqueue.assert_not_called()


def test_arm_skips_already_stopped_row_even_if_caller_did_not_pre_filter():
	with patch("src.services.winback_sequencer.wo.enqueue") as mock_enqueue:
		action_ids = arm_winback_run(
			_ArmFakeSession(), "client_a",
			_row(stopped_at=datetime.now(timezone.utc), stop_reason="REPLY"),
			datetime.now(timezone.utc),
		)
	assert action_ids == []
	mock_enqueue.assert_not_called()


def test_arm_skips_row_with_no_email():
	with patch("src.services.winback_sequencer.wo.enqueue") as mock_enqueue:
		action_ids = arm_winback_run(_ArmFakeSession(), "client_a", _row(email=None), datetime.now(timezone.utc))
	assert action_ids == []
	mock_enqueue.assert_not_called()


def test_arm_resolves_booking_link_per_row_with_owner_prefill():
	"""GHL prefill must use THIS row's own owner_name/email, not a
	batch-level default — booking_link.resolve_owner_booking_link is called
	fresh per row."""
	captured_calls = []

	def _fake_resolve(session, client_id, *, name=None, email=None):
		captured_calls.append({"client_id": client_id, "name": name, "email": email})
		return None

	with (
		patch("src.services.winback_sequencer.wo.enqueue", return_value=SimpleNamespace(action_id="a1")),
		patch("src.services.winback_sequencer.resolve_owner_booking_link", side_effect=_fake_resolve),
	):
		arm_winback_run(
			_ArmFakeSession(), "client_a",
			_row(owner_name="Jane Doe", email="jane@example.com"),
			datetime.now(timezone.utc),
		)

	assert captured_calls == [{"client_id": "client_a", "name": "Jane Doe", "email": "jane@example.com"}]


# ---------------------------------------------------------------------------
# dispatch_winback_touch — mirrors test_sequence_orchestrator.py's shape
# ---------------------------------------------------------------------------

class _Sender:
	def __init__(self, message_id="<m@acme-out.com>", raises=None):
		self.message_id = message_id
		self.raises = raises
		self.calls = []

	def send(self, **kwargs):
		self.calls.append(kwargs)
		if self.raises:
			raise self.raises
		return SendResult(message_id=self.message_id)


def _mailbox():
	return SimpleNamespace(mailbox_id=7, mailbox_address="sales@acme.com", sending_domain="acme-out.com")


@pytest.fixture(autouse=True)
def _stub_unsubscribe():
	with (
		patch("src.services.winback_sequencer.unsubscribe_url", return_value="https://app.example.com/unsub?token=t"),
		patch("src.services.winback_sequencer.append_unsubscribe_footer", side_effect=lambda body, url: body),
	):
		yield


def test_missing_content_returns_no_content_and_does_not_send():
	sender = _Sender()
	result = dispatch_winback_touch(MagicMock(), _row(), "client_a", touch_step=1, sender=sender, subject=None, body=None)
	assert result.outcome == "NO_CONTENT"
	assert sender.calls == []


def test_compliance_block_when_row_already_stopped():
	sender = _Sender()
	stopped_row = _row(stopped_at=datetime.now(timezone.utc), stop_reason="REPLY")
	result = dispatch_winback_touch(MagicMock(), stopped_row, "client_a", touch_step=2, sender=sender, subject="S", body="B")
	assert result.outcome == "COMPLIANCE_BLOCK"
	assert sender.calls == []


def test_happy_path_sends_and_marks_sent():
	session = MagicMock()
	sender = _Sender(message_id="<abc@acme-out.com>")
	with (
		patch("src.services.winback_sequencer.get_active_mailbox_for_client", return_value=_mailbox()),
		patch("src.services.winback_sequencer._claim_touch", return_value="dispatch-uuid"),
		patch("src.services.winback_sequencer._mark_sent", return_value=True),
		patch("src.services.winback_sequencer.log_touch_dispatched") as mock_log,
	):
		result = dispatch_winback_touch(session, _row(), "client_a", touch_step=1, sender=sender, subject="S", body="B")
	assert result.outcome == "SENT"
	assert result.message_id == "<abc@acme-out.com>"
	assert len(sender.calls) == 1
	mock_log.assert_called_once()
	assert mock_log.call_args.kwargs["campaign_type"] == "WIN_BACK"


def test_all_mailboxes_capped_returns_volume_cap():
	from src.services.mailbox_dispatcher import AllMailboxesCapped

	with patch("src.services.winback_sequencer.get_active_mailbox_for_client", side_effect=AllMailboxesCapped("capped")):
		result = dispatch_winback_touch(MagicMock(), _row(), "client_a", touch_step=1, sender=_Sender(), subject="S", body="B")
	assert result.outcome == "VOLUME_CAP"


def test_already_claimed_returns_already_claimed_and_does_not_send():
	sender = _Sender()
	with (
		patch("src.services.winback_sequencer.get_active_mailbox_for_client", return_value=_mailbox()),
		patch("src.services.winback_sequencer._claim_touch", return_value=None),
	):
		result = dispatch_winback_touch(MagicMock(), _row(), "client_a", touch_step=1, sender=sender, subject="S", body="B")
	assert result.outcome == "ALREADY_CLAIMED"
	assert sender.calls == []
