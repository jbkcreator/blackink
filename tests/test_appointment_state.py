"""Unit tests for the appointment state machine (Subtask 1.1.1).

Pure — no live DB. Covers the two application-layer invariants the generated
is_billable column cannot express: the reschedule cap forcing LOST, and
opportunity_id / reschedule_count being preserved across a no-show recovery.
The DB-level checks (is_billable truth table, CHECK constraints, indices) live
in tests/test_appointment_ops_live.py.
"""
import pytest

from src.services import appointment_state as st


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
