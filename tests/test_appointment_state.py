"""Unit tests for the appointment state machine (Subtask 1.1.1).

Pure — no live DB. Covers the two application-layer invariants the generated
is_billable column cannot express: the reschedule cap forcing LOST, and
opportunity_id / reschedule_count being preserved across a no-show recovery.
The DB-level checks (is_billable truth table, CHECK constraints, indices) live
in tests/test_appointment_ops_live.py.
"""
from unittest.mock import MagicMock

import pytest

from src.services import appointment_state as st
from src.services.appointments import (
	begin_no_show_recovery_for_appointment,
	reschedule_appointment,
)


def test_first_two_reschedules_increment_and_stay_rescheduled():
	t1 = st.apply_reschedule(st.BOOKED, 0)
	assert (t1.state, t1.reschedule_count) == (st.RESCHEDULED, 1)

	t2 = st.apply_reschedule(t1.state, t1.reschedule_count)
	assert (t2.state, t2.reschedule_count) == (st.RESCHEDULED, 2)


def test_third_reschedule_forces_lost_without_incrementing_past_cap():
	# At the cap already (2 prior reschedules): the move that would make it 3
	# lands in LOST instead, count left at the cap.
	t3 = st.apply_reschedule(st.RESCHEDULED, 2)
	assert t3.state == st.LOST
	assert t3.reschedule_count == st.MAX_RESCHEDULES
	assert "cap" in t3.reason


def test_can_reschedule_gate_matches_apply_reschedule_outcome():
	assert st.can_reschedule(st.BOOKED, 0) is True
	assert st.can_reschedule(st.RESCHEDULED, 1) is True
	assert st.can_reschedule(st.RESCHEDULED, 2) is False  # next move would be LOST
	assert st.can_reschedule(st.DISPOSITIONED, 0) is False  # meeting already happened


def test_reschedule_from_terminal_or_completed_state_is_invalid():
	for bad in (st.LOST, st.DISPOSITIONED, st.ATTENDED):
		with pytest.raises(st.InvalidTransition):
			st.apply_reschedule(bad, 0)


def test_no_show_recovery_preserves_reschedule_count():
	# Recovery is not a reschedule — the count is carried through untouched, so a
	# recovered meeting still has its full remaining reschedule budget.
	t = st.begin_no_show_recovery(st.CONFIRMED_3H, 1)
	assert t.state == st.NO_SHOW_RECOVERY
	assert t.reschedule_count == 1


def test_no_show_recovery_rejects_unknown_state():
	with pytest.raises(st.InvalidTransition):
		st.begin_no_show_recovery("NOT_A_STATE", 0)


def test_opportunity_id_is_never_part_of_a_transition():
	# The Transition dataclass carries no opportunity_id field at all — the whole
	# point of invariant (2): the helpers cannot mint or change the billing
	# anchor, so a rescheduled/recovered row keeps the original by construction.
	t = st.apply_reschedule(st.BOOKED, 0)
	assert not hasattr(t, "opportunity_id")


# ── src/services/appointments.py — the production write path (PR #25 review) ──
# apply_reschedule()/begin_no_show_recovery() above are pure and had NO
# production caller before this module existed; these tests prove the actual
# write path delegates to them rather than reimplementing the rules, and that
# a forced-LOST reschedule never moves scheduled_for.


def _fake_session(state, reschedule_count):
	session = MagicMock()
	session.execute.return_value.first.return_value = MagicMock(
		state=state, reschedule_count=reschedule_count
	)
	return session


def test_reschedule_appointment_delegates_to_apply_reschedule_and_moves_scheduled_for():
	session = _fake_session(st.BOOKED, 0)
	transition = reschedule_appointment(
		session, client_id="acme", appointment_id="a1", new_scheduled_for="2026-02-01"
	)
	assert (transition.state, transition.reschedule_count) == (st.RESCHEDULED, 1)
	update_sql = str(session.execute.call_args_list[-1][0][0])
	update_params = session.execute.call_args_list[-1][0][1]
	assert "scheduled_for" in update_sql
	assert update_params["scheduled_for"] == "2026-02-01"
	assert update_params["state"] == st.RESCHEDULED
	assert "opportunity_id" not in update_sql


def test_reschedule_appointment_third_attempt_lands_in_lost_not_an_error():
	# At the cap already — the production write path must actually succeed and
	# persist LOST, not raise (this is finding 1: previously nothing called
	# apply_reschedule() in production, so a real third reschedule only ever
	# hit the migration trigger's RAISE EXCEPTION).
	session = _fake_session(st.RESCHEDULED, 2)
	transition = reschedule_appointment(
		session, client_id="acme", appointment_id="a1", new_scheduled_for="2026-02-01"
	)
	assert transition.state == st.LOST
	assert transition.reschedule_count == st.MAX_RESCHEDULES
	update_sql = str(session.execute.call_args_list[-1][0][0])
	update_params = session.execute.call_args_list[-1][0][1]
	# scheduled_for is NEVER moved on the forced-LOST branch — there is no new
	# meeting to move to.
	assert "scheduled_for" not in update_sql
	assert update_params["state"] == st.LOST


def test_reschedule_appointment_raises_for_unknown_appointment():
	session = MagicMock()
	session.execute.return_value.first.return_value = None
	with pytest.raises(st.InvalidTransition):
		reschedule_appointment(session, client_id="acme", appointment_id="missing")


def test_begin_no_show_recovery_for_appointment_delegates_and_persists():
	session = _fake_session(st.CONFIRMED_3H, 1)
	transition = begin_no_show_recovery_for_appointment(
		session, client_id="acme", appointment_id="a1"
	)
	assert transition.state == st.NO_SHOW_RECOVERY
	assert transition.reschedule_count == 1
	update_params = session.execute.call_args_list[-1][0][1]
	assert update_params["state"] == st.NO_SHOW_RECOVERY
	assert update_params["reschedule_count"] == 1
