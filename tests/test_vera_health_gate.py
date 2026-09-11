"""Unit tests for S-1 — the Vera health gate and its wiring into every
settlement/billing sweep.

No live DB required: src.agents.vera.health_gate.evaluate_settlement_health
opens its own get_system_db_context() internally (see that module's
docstring for why), so every test here patches
"src.agents.vera.health_gate.get_system_db_context" directly rather than
touching a real database — and the sweep-level tests patch each sweep
module's OWN get_system_db_context reference to prove it is never reached
while halted, which only works because the two are genuinely separate
imports (see health_gate.py's module docstring).
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from src.agents.vera.health_gate import (
    HALT_ABSTAIN,
    HALT_NO_HEALTH_RUN,
    HALT_STALE_HEALTH_RUN,
    evaluate_settlement_health,
)


def _fake_db_returning(row):
    @contextmanager
    def _ctx():
        class _FakeSession:
            def execute(self, *a, **kw):
                return SimpleNamespace(fetchone=lambda: row)

        yield _FakeSession()

    return _ctx


def _fake_db_raising(exc):
    @contextmanager
    def _ctx():
        raise exc
        yield  # pragma: no cover - unreachable, keeps this a generator

    return _ctx


def _health_row(*, pm="VALUE", campaign="VALUE", pipeline="VALUE", ran_at=None):
    return SimpleNamespace(
        ran_at=ran_at or datetime.now(timezone.utc),
        overall="OK",
        pm_feed_state=pm,
        campaign_feed_state=campaign,
        pipeline_state=pipeline,
    )


# ── evaluate_settlement_health() — the gate's own decision logic ────────────

def test_no_health_run_halts():
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_returning(None)):
        decision = evaluate_settlement_health()
    assert decision.ok is False
    assert decision.reason == HALT_NO_HEALTH_RUN


def test_db_error_reading_health_runs_halts_as_no_health_run():
    """A failure to even read vera_health_runs must fail closed — same
    "could not verify -> do not assume healthy" posture as every check
    function's own ABSTAIN-on-DB-error behavior."""
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_raising(RuntimeError("db down"))):
        decision = evaluate_settlement_health()
    assert decision.ok is False
    assert decision.reason == HALT_NO_HEALTH_RUN


def test_stale_run_halts():
    old_row = _health_row(ran_at=datetime.now(timezone.utc) - timedelta(minutes=31))
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_returning(old_row)):
        decision = evaluate_settlement_health(as_of=datetime.now(timezone.utc))
    assert decision.ok is False
    assert decision.reason == HALT_STALE_HEALTH_RUN


def test_run_inside_staleness_window_does_not_halt_on_age_alone():
    fresh_row = _health_row(ran_at=datetime.now(timezone.utc) - timedelta(minutes=29))
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_returning(fresh_row)):
        decision = evaluate_settlement_health(as_of=datetime.now(timezone.utc))
    assert decision.ok is True
    assert decision.reason is None


def test_exact_30_minute_boundary_is_not_stale():
    """The precise threshold, not just "clearly inside/outside": age > max_age
    is a strict inequality, so a run exactly settings.vera_health_max_age_minutes
    (default 30) old is still healthy — confirmed against a real Postgres run
    in this same session (see 2026-09-10 testing-verification evidence)."""
    now = datetime.now(timezone.utc)
    row = _health_row(ran_at=now - timedelta(minutes=30))
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_returning(row)):
        decision = evaluate_settlement_health(as_of=now)
    assert decision.ok is True, decision


def test_one_second_past_30_minutes_is_stale():
    now = datetime.now(timezone.utc)
    row = _health_row(ran_at=now - timedelta(minutes=30, seconds=1))
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_returning(row)):
        decision = evaluate_settlement_health(as_of=now)
    assert decision.ok is False
    assert decision.reason == HALT_STALE_HEALTH_RUN


def test_abstain_halts():
    row = _health_row(pm="ABSTAIN")
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_returning(row)):
        decision = evaluate_settlement_health()
    assert decision.ok is False
    assert decision.reason == HALT_ABSTAIN
    assert "pm_feed" in decision.abstaining_checks


def test_multiple_abstains_all_named():
    row = _health_row(pm="ABSTAIN", pipeline="ABSTAIN")
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_returning(row)):
        decision = evaluate_settlement_health()
    assert decision.ok is False
    assert set(decision.abstaining_checks) == {"pm_feed", "pipeline"}


def test_unknown_campaign_feed_does_not_halt():
    """Pins the deliberate decision (plan §4 C1 / §7 Q-A): UNKNOWN means
    "never configured" (true of campaign_feed/Instantly today, per B8/B12
    in the implementation audit) and must NOT halt settlement/billing —
    only ABSTAIN ("configured but failed") does."""
    row = _health_row(campaign="UNKNOWN")
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_returning(row)):
        decision = evaluate_settlement_health()
    assert decision.ok is True
    assert decision.reason is None


def test_all_value_is_healthy():
    with patch("src.agents.vera.health_gate.get_system_db_context", _fake_db_returning(_health_row())):
        decision = evaluate_settlement_health()
    assert decision.ok is True


# ── Every gated sweep function halts, and none touches its own DB session
#    or an external provider, while the gate says HALT ─────────────────────

_SETTLEMENT_SWEEPS = [
    "run_door_signed_sweep",
    "run_installment_1_sweep",
    "run_installment_2_sweep",
]
_BILLING_SWEEPS = [
    "run_dispute_credit_sweep",
    "run_guarantee_sweep",
    "run_sit_invoice_sweep",
]


def _assert_sweep_short_circuits_on_halt(module_path: str, func_name: str, *, extra_patches=None):
    import importlib

    module = importlib.import_module(module_path)
    extra_patches = extra_patches or []

    def _raise_if_opened():
        raise AssertionError(f"{module_path}.{func_name} opened a DB session while HALTED")

    halted = SimpleNamespace(ok=False, reason=HALT_ABSTAIN, detail="test-forced halt", abstaining_checks=("pm_feed",))

    with patch(f"{module_path}.evaluate_settlement_health", return_value=halted), \
         patch.object(module, "get_system_db_context", side_effect=_raise_if_opened):
        from contextlib import ExitStack
        with ExitStack() as stack:
            for target, value in extra_patches:
                stack.enter_context(patch(target, value))
            result = getattr(module, func_name)()

    assert result == 0, f"{func_name} must return 0 while halted, got {result!r}"


@pytest.mark.parametrize("func_name", _SETTLEMENT_SWEEPS)
def test_settlement_sweeps_halt_and_never_open_db_session(func_name):
    _assert_sweep_short_circuits_on_halt("src.tasks.settlement_sweep", func_name)


@pytest.mark.parametrize("func_name", _BILLING_SWEEPS)
def test_billing_sweeps_halt_and_never_open_db_session(func_name):
    _assert_sweep_short_circuits_on_halt("src.tasks.billing_sweep", func_name)


def test_miss_credit_sweep_halts_and_never_opens_db_session_when_enabled_and_unhealthy(monkeypatch):
    """run_miss_credit_sweep has its own earlier fail-closed flag check
    (BILLING_MISS_CREDIT_SWEEP_ENABLED) — this test enables that flag so the
    health gate is actually the thing under test, not the flag."""
    from config.settings import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("BILLING_MISS_CREDIT_SWEEP_ENABLED", "true")
    try:
        _assert_sweep_short_circuits_on_halt("src.tasks.billing_sweep", "run_miss_credit_sweep")
    finally:
        get_settings.cache_clear()
