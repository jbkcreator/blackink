"""PR #53 review finding 1 — demo tenants must never inflate platform metrics.

Structural regression: both platform-wide aggregate queries (the executive
Slack digest and the internal metrics endpoint) must exclude every demo/test
client_id via the central DEMO_CLIENT_IDS registry, not a single hard-coded
literal. If a new demo tenant is added to the registry it is excluded from both
aggregates automatically; if either query stops honouring the registry this
test fails.
"""

from src.api import metrics_router
from src.core.demo_clients import DEMO_CLIENT_IDS
from src.tasks import daily_digest


def test_client_wins_demo_tenant_is_registered():
    # The S-21 seed creates DEMO_CLIENT_WINS with synthetic meeting_booked
    # events; it must be in the exclusion registry.
    assert "DEMO_CLIENT_WINS" in DEMO_CLIENT_IDS
    assert "DEMO_FRIDAY_SANDBOX" in DEMO_CLIENT_IDS


def test_digest_query_excludes_demo_clients_via_registry():
    sql = daily_digest._METRICS_SQL
    assert "client_id <> ALL(:demo_client_ids)" in sql
    # the old single-literal filter must be gone (it would miss DEMO_CLIENT_WINS)
    assert "client_id != 'DEMO_FRIDAY_SANDBOX'" not in sql


def test_metrics_endpoint_query_excludes_demo_clients_via_registry():
    sql = metrics_router._METRICS_SQL
    assert "client_id <> ALL(:demo_client_ids)" in sql
    assert "client_id <> 'DEMO_FRIDAY_SANDBOX'" not in sql
