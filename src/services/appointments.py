"""The single production write path for appointment reschedule / no-show
recovery (PR #25 review fix).

`src/services/appointment_state.py` defines the pure transition rules
(reschedule capped at 2 -> LOST, opportunity_id retained) but is DB-agnostic
on purpose and has no caller of its own — before this module existed, nothing
in production actually invoked `apply_reschedule()` or
`begin_no_show_recovery()`. The only thing a real reschedule hit was
`migrations/apply_appointment_ops.py`'s trigger, which raises on
`reschedule_count > 2` rather than ever landing the appointment in `LOST`.
These functions are that missing caller: they read the current row, apply the
pure rule, and issue the one UPDATE every caller should use, so the trigger's
`> 2` guard stays a backstop against a stray hand-written UPDATE rather than
the only thing a legitimate third reschedule ever reaches.

Runtime SQL uses sqlalchemy.text() with named binds per the repo's tooling
rules — no ORM query API. The caller owns the transaction (commit/rollback);
these functions issue statements on the session handed to them, mirroring
every other src/services/*.py write helper in this repo.
"""

from __future__ import annotations

from sqlalchemy import text

from src.services.appointment_state import (
	InvalidTransition,
	Transition,
	apply_reschedule,
	begin_no_show_recovery,
)


def reschedule_appointment(
	session,
	*,
	client_id: str,
	appointment_id: str,
	new_scheduled_for=None,
) -> Transition:
	"""Reschedule one appointment, applying the reschedule-cap rule.

	`FOR UPDATE` serializes concurrent reschedules of the SAME row, so two
	racing reschedule attempts can't both read reschedule_count=1 and both
	land on RESCHEDULED instead of one of them correctly forcing LOST.

	On the branch that would be the third reschedule, `apply_reschedule()`
	returns state=LOST with reschedule_count left at 2 (never incremented to
	3) — `new_scheduled_for` is deliberately NOT applied in that case, since a
	LOST appointment has no new meeting to move to. opportunity_id is never
	in the SET list: it is the exactly-once billing anchor and the trigger
	itself rejects any change to it.
	"""
	row = session.execute(
		text(
			"SELECT state, reschedule_count FROM appointments "
			"WHERE client_id = :client_id AND appointment_id = :appointment_id "
			"FOR UPDATE"
		),
		{"client_id": client_id, "appointment_id": appointment_id},
	).first()
	if row is None:
		raise InvalidTransition(
			f"no appointment {appointment_id!r} for client {client_id!r}"
		)

	transition = apply_reschedule(row.state, row.reschedule_count)

	if transition.state == "RESCHEDULED" and new_scheduled_for is not None:
		session.execute(
			text(
				"UPDATE appointments SET state = :state, "
				"reschedule_count = :reschedule_count, "
				"scheduled_for = :scheduled_for "
				"WHERE client_id = :client_id AND appointment_id = :appointment_id"
			),
			{
				"state": transition.state,
				"reschedule_count": transition.reschedule_count,
				"scheduled_for": new_scheduled_for,
				"client_id": client_id,
				"appointment_id": appointment_id,
			},
		)
	else:
		session.execute(
			text(
				"UPDATE appointments SET state = :state, "
				"reschedule_count = :reschedule_count "
				"WHERE client_id = :client_id AND appointment_id = :appointment_id"
			),
			{
				"state": transition.state,
				"reschedule_count": transition.reschedule_count,
				"client_id": client_id,
				"appointment_id": appointment_id,
			},
		)

	return transition


def begin_no_show_recovery_for_appointment(
	session,
	*,
	client_id: str,
	appointment_id: str,
) -> Transition:
	"""Move a missed appointment into NO_SHOW_RECOVERY.

	Same zero-production-caller gap as reschedule_appointment above:
	begin_no_show_recovery() carries the identical opportunity_id-retention
	invariant but nothing wired it to an actual write. reschedule_count is
	carried through unchanged (recovery is not a reschedule); opportunity_id
	is never touched, same reasoning as above.
	"""
	row = session.execute(
		text(
			"SELECT state, reschedule_count FROM appointments "
			"WHERE client_id = :client_id AND appointment_id = :appointment_id "
			"FOR UPDATE"
		),
		{"client_id": client_id, "appointment_id": appointment_id},
	).first()
	if row is None:
		raise InvalidTransition(
			f"no appointment {appointment_id!r} for client {client_id!r}"
		)

	transition = begin_no_show_recovery(row.state, row.reschedule_count)

	session.execute(
		text(
			"UPDATE appointments SET state = :state, "
			"reschedule_count = :reschedule_count "
			"WHERE client_id = :client_id AND appointment_id = :appointment_id"
		),
		{
			"state": transition.state,
			"reschedule_count": transition.reschedule_count,
			"client_id": client_id,
			"appointment_id": appointment_id,
		},
	)

	return transition
