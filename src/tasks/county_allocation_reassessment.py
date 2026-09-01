"""County-allocation reassessment sweep.

Adapted from the blueprint's Metro Allocation Algorithm (county-scoped per
the client's correction): "target owners are allocated to one client
campaign at a time based on portfolio size and operational fit. If outreach
remains unacted upon for 30 days, allocation re-evaluates via an automated
timer." Distinct from the permanent client_pm_books non-poach lock — see
src/core/models.py's CountyAllocation/ClientPmBook docstrings.

Two responsibilities:
  1. Initial allocation — a county with companies but no active allocation
     gets one, scored by aggregate portfolio size (sum of door_count_est)
     among clients with a seated interest in that county (see
     _score_candidate_clients — the actual "operational fit" scoring model
     is a business decision the plan flags as open; this is a placeholder
     ranking by portfolio size only, swappable later).
  2. Reassessment — any allocation past its reassess_after with no
     engagement event (a touch_sent/reply_received-class Event, once the
     Campaign Agent workstream lands) since allocation gets superseded and
     the county re-scored.

Runs under the BYPASSRLS system session — allocation decisions span
multiple clients by nature, no single tenant context applies.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.database import get_system_db_context

logger = logging.getLogger(__name__)


def _score_candidate_clients(session: Session, county_slug: str) -> list:
	"""Placeholder operational-fit scoring: clients with an active
	county_allocations row history in this county (i.e. have previously
	shown interest / been allocated here), ranked by their existing PM
	book size as a proxy for portfolio capacity. Real scoring criteria are
	a business decision flagged open in the Dev 1 plan."""
	rows = session.execute(
		text(
			"SELECT c.client_id, COUNT(b.id) AS book_size "
			"FROM clients c "
			"JOIN county_allocations ca ON ca.client_id = c.client_id AND ca.county_slug = :county "
			"LEFT JOIN client_pm_books b ON b.client_id = c.client_id "
			"WHERE c.is_active = TRUE AND c.client_id <> '_platform_internal' "
			"GROUP BY c.client_id ORDER BY book_size DESC"
		),
		{"county": county_slug},
	).fetchall()
	return [r.client_id for r in rows]


def _allocate(session: Session, county_slug: str, client_id: str, reason: str) -> None:
	now = datetime.now(timezone.utc)
	reassess_after = now + timedelta(days=get_settings().county_allocation_reassess_days)
	session.execute(
		text(
			"UPDATE county_allocations SET superseded_at = NOW() "
			"WHERE county_slug = :county AND superseded_at IS NULL"
		),
		{"county": county_slug},
	)
	session.execute(
		text(
			"INSERT INTO county_allocations (county_slug, client_id, allocated_at, reassess_after, allocation_reason) "
			"VALUES (:county, :client_id, :allocated_at, :reassess_after, :reason)"
		),
		{
			"county": county_slug,
			"client_id": client_id,
			"allocated_at": now,
			"reassess_after": reassess_after,
			"reason": reason,
		},
	)
	# owning_client_id is a materialized reflection of the county's current
	# active allocation, refreshed here rather than resolved via join at
	# query time — keeps the RLS policy on companies a simple column
	# comparison instead of a subquery against county_allocations.
	session.execute(
		text("UPDATE companies SET owning_client_id = :client_id WHERE county_slug = :county"),
		{"client_id": client_id, "county": county_slug},
	)


def run_initial_allocation_sweep() -> int:
	"""Counties with promoted companies but no active allocation."""
	allocated = 0
	with get_system_db_context() as session:
		unallocated_counties = session.execute(
			text(
				"SELECT DISTINCT co.county_slug FROM companies co "
				"WHERE co.owning_client_id IS NULL "
				"AND NOT EXISTS ("
				"  SELECT 1 FROM county_allocations ca "
				"  WHERE ca.county_slug = co.county_slug AND ca.superseded_at IS NULL"
				")"
			)
		).fetchall()

		for row in unallocated_counties:
			candidates = _score_candidate_clients(session, row.county_slug)
			if not candidates:
				continue
			_allocate(session, row.county_slug, candidates[0], "initial allocation - highest scored candidate")
			allocated += 1

	logger.info("county_allocation: initial sweep allocated %d counties", allocated)
	return allocated


def run_reassessment_sweep() -> int:
	reassessed = 0
	with get_system_db_context() as session:
		due = session.execute(
			text(
				"SELECT county_slug, client_id FROM county_allocations "
				"WHERE superseded_at IS NULL AND reassess_after <= NOW()"
			)
		).fetchall()

		for row in due:
			# No engagement mechanism exists yet (Campaign Agent workstream)
			# — every due allocation is re-scored from scratch, conservatively.
			candidates = _score_candidate_clients(session, row.county_slug)
			if not candidates:
				continue
			if candidates[0] == row.client_id:
				# Same top candidate — extend the window rather than
				# create a redundant superseded/re-allocated pair.
				new_reassess_after = datetime.now(timezone.utc) + timedelta(
					days=get_settings().county_allocation_reassess_days
				)
				session.execute(
					text(
						"UPDATE county_allocations SET reassess_after = :new_after "
						"WHERE county_slug = :county AND superseded_at IS NULL"
					),
					{"new_after": new_reassess_after, "county": row.county_slug},
				)
				continue
			_allocate(session, row.county_slug, candidates[0], "reassessment - 30-day window elapsed")
			reassessed += 1

	logger.info("county_allocation: reassessment sweep changed %d counties", reassessed)
	return reassessed


if __name__ == "__main__":
	logging.basicConfig(level=logging.INFO)
	run_initial_allocation_sweep()
	run_reassessment_sweep()
