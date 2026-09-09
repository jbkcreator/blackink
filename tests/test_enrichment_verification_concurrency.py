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

Client scoping uses the repo's own canary-tenant fixture
(tests/fixtures/synthetic_tenants.py), not a hand-picked client_id — an
earlier draft hardcoded "DEMO_FRIDAY_SANDBOX", a client that only exists on
the shared remote server this branch was manually smoke-tested against, not
in CI's fresh database (confirmed the hard way: CI failed with a
winback_imports_client_id_fkey violation). CANARY_A is freshly seeded by
the canary_tenants fixture in every environment, CI included.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from src.core.database import get_owner_db_context, get_system_db_context
from src.tasks.enrichment_verification import _claim_rows, _mark_claimed
from tests.fixtures.synthetic_tenants import CANARY_A, canary_tenants  # noqa: F401


@pytest.fixture
def synthetic_winback_row(canary_tenants):  # noqa: F811 — pytest fixture injection, not a redefinition
	"""Inserts one real winback_imports + winback_rows row under the
	canary_tenants fixture's CANARY_A client (freshly seeded in every
	environment, unlike a hand-picked sandbox client_id), yields its
	winback_row_id, then deletes both rows.

	Teardown uses get_owner_db_context(), NOT get_system_db_context() —
	confirmed the hard way in CI: blackink_system (what get_system_db_context
	uses, and what _claim_rows/_mark_claimed correctly run as in production)
	was never granted DELETE on winback_imports/winback_rows/
	winback_gate_checks (migrations/apply_winback_imports.py,
	apply_winback_touch_sequence.py — unlike clients/companies/contacts,
	which DO grant blackink_system DELETE; nothing in production ever
	deletes a winback row, so this gap never mattered until a TEST needed
	to). A DELETE that fails with InsufficientPrivilege aborts the whole
	teardown transaction before the later statements even run, leaving the
	row behind — which then made canary_tenants' own teardown fail with a
	winback_imports_client_id_fkey violation trying to delete the
	now-orphaned _leakcanary_a client, cascading into every later test that
	shares that fixture for the rest of the CI run. The owner role (same one
	every migrations/apply_*.py script uses) has no such restriction and is
	the correct tool for test-only cleanup that a normal app role can't do.
	winback_gate_checks itself is never written here (only
	evaluate_winback_touch_gate does that, which this test never calls) —
	dropped that delete rather than fixing its privilege, since there is
	nothing there to clean up."""
	import_id = str(uuid.uuid4())
	now = datetime.now(timezone.utc)
	with get_system_db_context() as session:
		session.execute(
			text(
				"INSERT INTO winback_imports (import_id, client_id, filename, uploaded_by, status, created_at) "
				"VALUES (:iid, :cid, 'concurrency_test.csv', 'pytest', 'COMPLETED', :now)"
			),
			{"iid": import_id, "cid": CANARY_A, "now": now},
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
			{"iid": import_id, "cid": CANARY_A, "now": now},
		).scalar_one()
		session.commit()

	yield row_id

	with get_owner_db_context() as session:
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
		rows_a = _claim_rows(session_a, CANARY_A, None, max_attempts=3, limit=10, now=now)
		assert row_id in [r.winback_row_id for r in rows_a], "session A must claim the row first"
		_mark_claimed(session_a, [r.winback_row_id for r in rows_a], now)
		session_a.commit()

	# Session B: a genuinely separate connection/transaction, claiming
	# immediately after session A's lock was released. Without the lease
	# fix, the row would still show enrichment_timestamp IS NULL and
	# enrichment_attempts under budget -- fully re-claimable here, which is
	# exactly the duplicate-submission race the PR review caught.
	with get_system_db_context() as session_b:
		rows_b = _claim_rows(session_b, CANARY_A, None, max_attempts=3, limit=10, now=now)
		assert row_id not in [r.winback_row_id for r in rows_b], (
			"session B re-claimed a row session A already leased -- the concurrency fix regressed"
		)


def test_row_becomes_reclaimable_again_once_the_lease_expires(synthetic_winback_row):
	row_id = synthetic_winback_row
	now = datetime.now(timezone.utc)

	with get_system_db_context() as session_a:
		rows_a = _claim_rows(session_a, CANARY_A, None, max_attempts=3, limit=10, now=now)
		_mark_claimed(session_a, [r.winback_row_id for r in rows_a], now)
		session_a.commit()

	# A crash-recovery check, not a real 15-minute wait: pass a `now` that is
	# already past the lease window to _claim_rows's own lease_cutoff
	# computation, proving a genuinely abandoned lease does not strand the
	# row forever.
	future = now + timedelta(minutes=20)
	with get_system_db_context() as session_b:
		rows_b = _claim_rows(session_b, CANARY_A, None, max_attempts=3, limit=10, now=future)
		assert row_id in [r.winback_row_id for r in rows_b], (
			"a row whose lease has genuinely expired must become re-claimable — crash recovery would otherwise strand it forever"
		)
