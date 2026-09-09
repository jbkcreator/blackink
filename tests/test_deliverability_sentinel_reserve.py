"""Integration tests for the deliverability sentinel's reserve-domain swap
(PR #35 review #2). Requires a live Postgres with migrations applied — skipped
otherwise, same posture as tests/test_tenant_isolation.py.

These pin the reserve model down: a degraded tenant domain is only replaced by a
reserve in the SAME cluster AND the SAME tenant. In particular they prove the
reviewer's concern with the NULL-scoped (global) reserve inventory — a global
reserve does NOT rescue a tenant domain — so reserves MUST be provisioned with
their owning client_id + cluster_label before the swap can fire in the pilot.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import text

try:
    from src.core.database import get_owner_db_context
    from src.tasks.deliverability_sentinel import Trip, _quarantine_and_swap

    with get_owner_db_context() as _s:
        _s.execute(text("SELECT 1 FROM sending_domains LIMIT 1"))
    _DB_OK = True
except Exception:  # noqa: BLE001 — no DB / no migrations → skip the module
    _DB_OK = False

pytestmark = pytest.mark.skipif(not _DB_OK, reason="requires live Postgres with migrations applied")

_CLIENT = "SENTINEL_RESERVE_TEST"


def _exec(sql: str, **params):
    with get_owner_db_context() as s:
        s.execute(text(sql), params)
        s.commit()


def _query(sql: str, **params):
    with get_owner_db_context() as s:
        return s.execute(text(sql), params).fetchall()


def _cleanup():
    _exec("DELETE FROM sending_domains WHERE domain LIKE 'sentinel-reserve-test-%'")
    _exec("DELETE FROM clients WHERE client_id = :c", c=_CLIENT)


def _seed_client():
    _exec(
        "INSERT INTO clients (client_id, display_name, is_active) VALUES (:c, 'Sentinel Reserve Test', TRUE) "
        "ON CONFLICT (client_id) DO NOTHING",
        c=_CLIENT,
    )


def _add_domain(domain, client_id, cluster, state, is_reserve):
    _exec(
        "INSERT INTO sending_domains (domain, client_id, cluster_label, quarantine_state, is_reserve) "
        "VALUES (:d, :c, :cl, :st, :r)",
        d=domain, c=client_id, cl=cluster, st=state, r=is_reserve,
    )


def _row(domain):
    r = _query(
        "SELECT id, domain, cluster_label, client_id, quarantine_state, is_reserve "
        "FROM sending_domains WHERE domain = :d",
        d=domain,
    )
    return r[0] if r else None


def _run_swap(active_domain):
    d = _row(active_domain)
    domain_row = SimpleNamespace(id=d.id, domain=d.domain, cluster_label=d.cluster_label, client_id=d.client_id)
    trip = Trip(domain=d.domain, rule="bounce_rate", observed_value=9.9, threshold=3.0)
    with get_owner_db_context() as s:
        _quarantine_and_swap(s, domain_row, trip)
        s.commit()


@pytest.fixture(autouse=True)
def _around():
    _cleanup()
    _seed_client()
    yield
    _cleanup()


def test_global_reserve_does_not_rescue_a_tenant_domain():
    """The reviewer's NULL-scoped inventory case: a global reserve (client_id
    NULL, cluster NULL) is NOT promoted for a tenant domain — the tenant domain
    is quarantined with no active replacement. Documents why reserves must be
    provisioned per tenant+cluster before the pilot."""
    _add_domain("sentinel-reserve-test-active.com", _CLIENT, "clusterX", "active", False)
    _add_domain("sentinel-reserve-test-global.com", None, None, "reserve", True)

    _run_swap("sentinel-reserve-test-active.com")

    assert _row("sentinel-reserve-test-active.com").quarantine_state == "quarantined"
    # Global reserve untouched — still a reserve, never promoted.
    g = _row("sentinel-reserve-test-global.com")
    assert g.quarantine_state == "reserve" and g.is_reserve is True


def test_same_cluster_same_tenant_reserve_is_promoted():
    """A reserve provisioned with the failed domain's cluster AND client_id is
    promoted to active — the correct provisioning shape."""
    _add_domain("sentinel-reserve-test-active.com", _CLIENT, "clusterX", "active", False)
    _add_domain("sentinel-reserve-test-reserve.com", _CLIENT, "clusterX", "reserve", True)

    _run_swap("sentinel-reserve-test-active.com")

    assert _row("sentinel-reserve-test-active.com").quarantine_state == "quarantined"
    promoted = _row("sentinel-reserve-test-reserve.com")
    assert promoted.quarantine_state == "active" and promoted.is_reserve is False


def test_wrong_cluster_reserve_is_not_promoted():
    """A same-tenant reserve in a DIFFERENT cluster is not promoted — no
    cross-cluster reputation bleed."""
    _add_domain("sentinel-reserve-test-active.com", _CLIENT, "clusterX", "active", False)
    _add_domain("sentinel-reserve-test-other.com", _CLIENT, "clusterY", "reserve", True)

    _run_swap("sentinel-reserve-test-active.com")

    other = _row("sentinel-reserve-test-other.com")
    assert other.quarantine_state == "reserve" and other.is_reserve is True


def test_internal_pool_matches_its_own_null_reserve():
    """The internal self-marketing pool (client_id NULL) DOES match a NULL-scoped
    reserve — IS NOT DISTINCT FROM lets NULL match NULL for its own pool."""
    _add_domain("sentinel-reserve-test-internal.com", None, None, "active", False)
    _add_domain("sentinel-reserve-test-internal-reserve.com", None, None, "reserve", True)

    _run_swap("sentinel-reserve-test-internal.com")

    promoted = _row("sentinel-reserve-test-internal-reserve.com")
    assert promoted.quarantine_state == "active" and promoted.is_reserve is False
