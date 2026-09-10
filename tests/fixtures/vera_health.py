"""S-1 test support — seeds one healthy vera_health_runs row.

Every settlement/billing sweep function now calls
src.agents.vera.health_gate.evaluate_settlement_health() before doing any
work (see src/tasks/settlement_sweep.py / src/tasks/billing_sweep.py). A
live-DB test that exercises one of those sweeps against a freshly migrated
test database has no vera_health_runs row at all, which the gate correctly
reads as NO_HEALTH_RUN and halts on — exactly the fail-closed behavior the
gate exists for, but not what a test of, say, sit-invoice billing logic is
trying to exercise. This fixture seeds a single fresh, healthy row so those
tests exercise their own logic rather than the health gate.

Deliberately a separate, narrowly-used fixture rather than folded into
tests/fixtures/synthetic_tenants.py::canary_tenants — that fixture backs
~30 unrelated tenant-isolation tests in test_tenant_isolation.py, and
vera_health_runs is not tenant-scoped data, so it has no business being a
side effect of every canary tenant setup.

campaign_feed_state is UNKNOWN, not VALUE — this mirrors today's real
platform state (Instantly has no API key configured anywhere in this repo's
test or dev environment) and is a live, intentional check that UNKNOWN does
not halt (see health_gate.py's module docstring and
tests/test_vera_health_gate.py::test_unknown_campaign_feed_does_not_halt for
the same assertion in isolation).
"""

import pytest
from sqlalchemy import text

from src.core.database import get_system_db_context


@pytest.fixture
def healthy_vera_run():
	with get_system_db_context() as session:
		session.execute(
			text("""
				INSERT INTO vera_health_runs (
					overall,
					pm_feed_state, pm_feed_detail,
					campaign_feed_state, campaign_feed_detail,
					pipeline_state, pipeline_detail
				) VALUES (
					'OK',
					'VALUE', 'seeded by tests/fixtures/vera_health.py::healthy_vera_run',
					'UNKNOWN', 'seeded — Instantly not configured, matches real test/dev state',
					'VALUE', 'seeded by tests/fixtures/vera_health.py::healthy_vera_run'
				)
			""")
		)
	yield
	# No teardown DELETE: vera_health_runs is not tenant-scoped and the gate
	# only ever reads the single latest row, so leaving old healthy rows
	# behind is harmless and keeps this fixture simple.
