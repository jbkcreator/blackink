"""Appointment state-machine rules (Subtask 1.1.1).

The `appointment_state_enum` transitions and their two hard invariants live
here, in the application layer, because a Postgres generated column can express
a *value* (see appointments.is_billable) but not a *transition guard*:

  1. Reschedule is capped at 2. A third reschedule attempt forces the
     appointment to LOST rather than incrementing reschedule_count past 2 — the
     billable meeting never materialized, so it exits the funnel. Expressed as
     "reschedule_count > 2 → LOST" in the blueprint; enforced here as "the move
     that *would* make it 3 lands in LOST instead."

  2. opportunity_id is preserved across every reschedule AND across no-show
     recovery. It is the exactly-once billing anchor — a new opportunity_id on
     a rescheduled/recovered meeting would let the same opportunity bill twice.
     These helpers never mint or change it; a caller that needs a *new*
     opportunity is not rescheduling.

Pure and DB-agnostic on purpose: every function takes the current
(state, reschedule_count) — or a lightweight object exposing them — and returns
the intended next values, so the whole state machine is unit-testable without a
live Postgres (the FakeSession pattern the rest of the suite uses). The caller
owns the actual UPDATE and its transaction.
"""

from __future__ import annotations

from dataclasses import dataclass

# The nine states of appointment_state_enum, in lifecycle order.
BOOKED = "BOOKED"
CONFIRMED_24H = "CONFIRMED_24H"
CONFIRMED_3H = "CONFIRMED_3H"
ATTENDED = "ATTENDED"
DISPOSITIONED = "DISPOSITIONED"
RESCHEDULED = "RESCHEDULED"
NO_SHOW_RECOVERY = "NO_SHOW_RECOVERY"
REBOOKED = "REBOOKED"
LOST = "LOST"

ALL_STATES = frozenset(
	{
		BOOKED,
		CONFIRMED_24H,
		CONFIRMED_3H,
		ATTENDED,
		DISPOSITIONED,
		RESCHEDULED,
		NO_SHOW_RECOVERY,
		REBOOKED,
		LOST,
	}
)

# The hard cap from the blueprint: at most two reschedules; the third forces LOST.
MAX_RESCHEDULES = 2

# States from which a reschedule is meaningful — a meeting that already happened
# (DISPOSITIONED) or already exited the funnel (LOST) cannot be rescheduled.
_RESCHEDULABLE = frozenset(
	{BOOKED, CONFIRMED_24H, CONFIRMED_3H, RESCHEDULED, NO_SHOW_RECOVERY, REBOOKED}
)


class InvalidTransition(ValueError):
	"""Raised for a transition the state machine forbids outright (as opposed to
	the reschedule cap, which resolves to LOST rather than erroring)."""


@dataclass(frozen=True)
class Transition:
	"""The intended result of a transition. `state` and `reschedule_count` are
	what the caller should persist; `reason` explains a cap-forced LOST so it can
	be logged. opportunity_id is deliberately absent — it never changes here."""

	state: str
	reschedule_count: int
	reason: str = ""


def apply_reschedule(current_state: str, reschedule_count: int) -> Transition:
	"""Reschedule an appointment.

	Returns a Transition to RESCHEDULED with reschedule_count + 1 while the
	appointment still has reschedules left, and a Transition to LOST (with
	reschedule_count left at MAX_RESCHEDULES) once it would exceed the cap. The
	caller keeps the SAME opportunity_id on the resulting row — invariant (2).
	"""
	if current_state not in _RESCHEDULABLE:
		raise InvalidTransition(
			f"cannot reschedule from state {current_state!r}"
		)
	if reschedule_count >= MAX_RESCHEDULES:
		# This would be the third reschedule — force LOST instead of a 3rd count.
		return Transition(
			state=LOST,
			reschedule_count=reschedule_count,
			reason=f"reschedule cap ({MAX_RESCHEDULES}) exceeded",
		)
	return Transition(state=RESCHEDULED, reschedule_count=reschedule_count + 1)


def begin_no_show_recovery(current_state: str, reschedule_count: int) -> Transition:
	"""Move a missed appointment into NO_SHOW_RECOVERY. reschedule_count is
	carried through unchanged (recovery is not a reschedule), and the caller MUST
	retain the original opportunity_id on the recovery row — invariant (2), the
	same anchor the recovered meeting bills under."""
	if current_state not in ALL_STATES:
		raise InvalidTransition(f"unknown appointment state {current_state!r}")
	return Transition(state=NO_SHOW_RECOVERY, reschedule_count=reschedule_count)


def can_reschedule(current_state: str, reschedule_count: int) -> bool:
	"""True when a further reschedule would produce a RESCHEDULED row rather than
	forcing LOST — handy for gating a UI action before attempting the move."""
	return current_state in _RESCHEDULABLE and reschedule_count < MAX_RESCHEDULES
