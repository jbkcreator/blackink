"""Two-session concurrency test for src/tasks/enrichment_verification.py's
claim lease (PR review finding, confirmed real): without a durable lease,
the FOR UPDATE SKIP LOCKED row lock from _claim_rows is released the moment
the enrichment_attempts bump commits — well before enrichment_timestamp is
ever set (that happens only after provider.collect() returns, which for a
real Tracerfy poll can take up to ~10 minutes) — so a second, concurrent
sweep session could re-claim and re-submit the same rows.

Requires a real Postgres instance with migrations 1-N applied, same posture
as tests/test_tenant_isolation.py ("requires a real Postgres... stays
required on every PR touching models.py / migrations/ / database.py") — not
gated behind a skip-if-no-DB check, matching that file's own convention.

Uses two independent DB sessions (get_system_db_context() twice — separate
connections, separate transactions) to prove session B's claim genuinely
excludes what session A already claimed and leased, not just that the two
calls happen not to overlap by accident.
"""

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_system_db_context
from src.tasks.enrichment_verification import _claim_rows, _mark_claimed

_TEST_CLIENT_ID = "DEMO_FRIDAY_SANDBOX"


@pytest.fixture
def synthetic_winback_row():
	"""Inserts one real winback_imports + winback_rows row under the
	existing DEMO_FRIDAY_SANDBOX sandbox client, yields its winback_row_id,
	then deletes both rows — same safe pattern already used to live-verify
	this subtask's migration (zero real client data touched)."""
	import_id = str(uuid.uuid4())
	now = datetime.now(timezone.utc)
	with get_system_db_context() as session:
		session.execute(
			text(
				"INSERT INTO winback_imports (import_id, client_id, filename, uploaded_by, status, created_at) "
				"VALUES (:iid, :cid, 'concurrency_test.csv', 'pytest', 'COMPLETED', :now)"
			),
			{"iid": import_id, "cid": _TEST_CLIENT_ID, "now": now},
		)
		row_id = session.execute(
			text(
				"INSERT INTO winback_rows (import_id, client_id, owner_name, property_address_raw, "
				"property_address_normalized, county_slug, phone, email, disposition, "
				"requires_human_review, suppression_state, created_at, updated_at) "
				"VALUES (:iid, :cid, 'Concurrency Test Owner', '1 Race Condition Ave', '1 RACE CONDITION AVE', "
				"NULL, NULL, 'concurrency-test@example.com', 'STILL_OWNS_STILL_RENTING', FALSE, FALSE, :now, :now) "
				"RETURNING winback_row_id"
			),
			{"iid": import_id, "cid": _TEST_CLIENT_ID, "now": now},
		).scalar_one()
		session.commit()

	yield row_id

	with get_system_db_context() as session:
		session.execute(text("DELETE FROM winback_gate_checks WHERE winback_row_id = :id"), {"id": row_id})
		session.execute(text("DELETE FROM winback_rows WHERE winback_row_id = :id"), {"id": row_id})
		session.execute(text("DELETE FROM winback_imports WHERE import_id = :iid"), {"iid": import_id})
		session.commit()


def test_second_sweep_cannot_reclaim_a_row_the_first_sweep_already_leased(synthetic_winback_row):
	row_id = synthetic_winback_row
	now = datetime.now(timezone.utc)

	# Session A: claims the row and stamps the lease, then commits and
	# releases the FOR UPDATE row lock -- exactly what run_sweep() does
	# before calling provider.submit()/collect(), i.e. before the up-to-
	# 10-minute vendor round trip that this whole test exists to simulate.
	with get_system_db_context() as session_a:
		rows_a = _claim_rows(session_a, _TEST_CLIENT_ID, None, max_attempts=3, limit=10, now=now)
		assert row_id in [r.winback_row_id for r in rows_a], "session A must claim the row first"
		_mark_claimed(session_a, [r.winback_row_id for r in rows_a], now)
		session_a.commit()

	# Session B: a genuinely separate connection/transaction, claiming
	# immediately after session A's lock was released. Without the lease
	# fix, the row would still show enrichment_timestamp IS NULL and
	# enrichment_attempts under budget -- fully re-claimable here, which is
	# exactly the duplicate-submission race the PR review caught.
	with get_system_db_context() as session_b:
		rows_b = _claim_rows(session_b, _TEST_CLIENT_ID, None, max_attempts=3, limit=10, now=now)
		assert row_id not in [r.winback_row_id for r in rows_b], (
			"session B re-claimed a row session A already leased -- the concurrency fix regressed"
		)


def test_row_becomes_reclaimable_again_once_the_lease_expires(synthetic_winback_row):
	row_id = synthetic_winback_row
	now = datetime.now(timezone.utc)

	with get_system_db_context() as session_a:
		rows_a = _claim_rows(session_a, _TEST_CLIENT_ID, None, max_attempts=3, limit=10, now=now)
		_mark_claimed(session_a, [r.winback_row_id for r in rows_a], now)
		session_a.commit()

	# A crash-recovery check, not a real 15-minute wait: pass a `now` that is
	# already past the lease window to _claim_rows's own lease_cutoff
	# computation, proving a genuinely abandoned lease does not strand the
	# row forever.
	from datetime import timedelta

	future = now + timedelta(minutes=20)
	with get_system_db_context() as session_b:
		rows_b = _claim_rows(session_b, _TEST_CLIENT_ID, None, max_attempts=3, limit=10, now=future)
		assert row_id in [r.winback_row_id for r in rows_b], (
			"a row whose lease has genuinely expired must become re-claimable — crash recovery would otherwise strand it forever"
		)
