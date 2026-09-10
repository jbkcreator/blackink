"""Unit tests for S-1's vera_health_sweep.py — the transition-detection and
alerting logic that had zero coverage in the first review pass (code-review
finding: run_sweep()/_persist_run()/_maybe_alert_and_log()/_result_by_name()
were only ever exercised by a deleted one-off live-DB script).

No live DB: get_system_db_context, post_notice, and log_event are all
patched. Every test resets src.tasks.vera_health_sweep._previous_ok
explicitly rather than relying on import order or test order.
"""
from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.vera.health_gate import HALT_ABSTAIN
from src.agents.vera.health_result import ABSTAIN, UNKNOWN, VALUE, HealthResult
from src.tasks import vera_health_sweep as sweep_mod


def _results(*, pm=VALUE, campaign=UNKNOWN, pipeline=VALUE):
    return [
        HealthResult("pm_feed", pm, {"x": 1} if pm == VALUE else None, "pm detail"),
        HealthResult("campaign_feed", campaign, {"x": 1} if campaign == VALUE else None, "campaign detail"),
        HealthResult("pipeline", pipeline, {"x": 1} if pipeline == VALUE else None, "pipeline detail"),
    ]


@contextmanager
def _fake_db(session):
    yield session


# ── _result_by_name ──────────────────────────────────────────────────────

def test_result_by_name_finds_the_right_check():
    results = _results()
    assert sweep_mod._result_by_name(results, "campaign_feed").check_name == "campaign_feed"


def test_result_by_name_raises_on_unknown_check_name():
    with pytest.raises(ValueError, match="no HealthResult named"):
        sweep_mod._result_by_name(_results(), "not_a_real_check")


# ── _persist_run ──────────────────────────────────────────────────────────

def test_persist_run_writes_ok_when_no_abstain():
    session = MagicMock()
    with patch.object(sweep_mod, "get_system_db_context", return_value=_fake_db(session)):
        overall = sweep_mod._persist_run(_results(pm=VALUE, campaign=UNKNOWN, pipeline=VALUE))
    assert overall == "OK"
    params = session.execute.call_args.args[1]
    assert params["overall"] == "OK"
    assert params["pm_feed_state"] == VALUE
    assert params["campaign_feed_state"] == UNKNOWN
    assert params["pipeline_state"] == VALUE


def test_persist_run_writes_halt_when_any_abstain():
    session = MagicMock()
    with patch.object(sweep_mod, "get_system_db_context", return_value=_fake_db(session)):
        overall = sweep_mod._persist_run(_results(pm=ABSTAIN))
    assert overall == "HALT"
    assert session.execute.call_args.args[1]["overall"] == "HALT"


# ── _maybe_alert_and_log ────────────────────────────────────────────────────

def _decision(ok, reason=None, detail="d", abstaining=()):
    from types import SimpleNamespace
    return SimpleNamespace(ok=ok, reason=reason, detail=detail, abstaining_checks=abstaining)


def test_transition_into_halt_posts_alert_and_writes_event(monkeypatch):
    session = MagicMock()
    monkeypatch.setattr(sweep_mod, "get_system_db_context", lambda: _fake_db(session))
    fake_post = AsyncMock(return_value="123.456")
    monkeypatch.setattr(sweep_mod, "post_notice", fake_post)
    logged = []
    monkeypatch.setattr(
        sweep_mod, "log_event",
        lambda client_id, event_type, **kw: logged.append((event_type, kw["payload"])),
    )

    sweep_mod._maybe_alert_and_log(True, _decision(False, reason=HALT_ABSTAIN, abstaining=("pm_feed",)))

    assert fake_post.await_count == 1
    assert fake_post.call_args.kwargs["channel_key"] == "qa"
    assert "HALTED" in fake_post.call_args.kwargs["text"]
    assert len(logged) == 1
    event_type, payload = logged[0]
    assert event_type == "vera_health_halt_issued"
    assert payload["reason"] == HALT_ABSTAIN
    assert payload["abstaining_checks"] == ["pm_feed"]


def test_transition_into_halt_writes_event_before_posting_to_slack(monkeypatch):
    """Event write must happen first — see the module's own docstring:
    a non-SlackApiError exception from post_notice must not prevent the
    halt from being durably recorded."""
    order = []
    monkeypatch.setattr(sweep_mod, "get_system_db_context", lambda: _fake_db(MagicMock()))
    monkeypatch.setattr(sweep_mod, "log_event", lambda *a, **kw: order.append("event"))
    fake_post = AsyncMock(side_effect=lambda **kw: order.append("slack"))
    monkeypatch.setattr(sweep_mod, "post_notice", fake_post)

    sweep_mod._maybe_alert_and_log(True, _decision(False, reason=HALT_ABSTAIN))

    assert order == ["event", "slack"]


def test_transition_into_recovery_posts_alert_but_writes_no_event(monkeypatch):
    fake_post = AsyncMock(return_value="123.456")
    monkeypatch.setattr(sweep_mod, "post_notice", fake_post)
    logged = []
    monkeypatch.setattr(sweep_mod, "log_event", lambda *a, **kw: logged.append(1))
    db_opened = []
    monkeypatch.setattr(
        sweep_mod, "get_system_db_context",
        lambda: (_ for _ in ()).throw(AssertionError("must not open a DB session on recovery")),
    )

    sweep_mod._maybe_alert_and_log(False, _decision(True))

    assert fake_post.await_count == 1
    assert "recovered" in fake_post.call_args.kwargs["text"]
    assert logged == []
    assert db_opened == []


def test_no_transition_posts_nothing_and_writes_no_event_while_still_halted(monkeypatch):
    fake_post = AsyncMock()
    monkeypatch.setattr(sweep_mod, "post_notice", fake_post)
    monkeypatch.setattr(
        sweep_mod, "log_event",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not log while state is unchanged")),
    )
    monkeypatch.setattr(
        sweep_mod, "get_system_db_context",
        lambda: (_ for _ in ()).throw(AssertionError("must not open a DB session while state is unchanged")),
    )

    sweep_mod._maybe_alert_and_log(False, _decision(False, reason=HALT_ABSTAIN))

    assert fake_post.await_count == 0


def test_no_transition_posts_nothing_while_still_healthy(monkeypatch):
    fake_post = AsyncMock()
    monkeypatch.setattr(sweep_mod, "post_notice", fake_post)

    sweep_mod._maybe_alert_and_log(True, _decision(True))

    assert fake_post.await_count == 0


# ── run_sweep — the full tick, wiring the pieces together ───────────────────

def test_run_sweep_updates_previous_ok_and_persists_a_row(monkeypatch):
    sweep_mod._previous_ok = True
    try:
        monkeypatch.setattr(sweep_mod, "run_health_checks", lambda: _results(pm=ABSTAIN))
        persisted = []
        monkeypatch.setattr(sweep_mod, "_persist_run", lambda results: persisted.append(results) or "HALT")
        monkeypatch.setattr(
            sweep_mod, "evaluate_settlement_health",
            lambda: _decision(False, reason=HALT_ABSTAIN),
        )
        alerted = []
        monkeypatch.setattr(
            sweep_mod, "_maybe_alert_and_log",
            lambda previous_ok, decision: alerted.append((previous_ok, decision.ok)),
        )

        sweep_mod.run_sweep()

        assert len(persisted) == 1
        assert alerted == [(True, False)]
        assert sweep_mod._previous_ok is False
    finally:
        sweep_mod._previous_ok = True


def test_run_sweep_second_tick_sees_updated_previous_ok(monkeypatch):
    """Confirms the module-global transition tracking actually carries state
    across two consecutive ticks, which is the entire point of _previous_ok
    existing — a bug here would mean either double-alerting forever or
    never alerting again after the first transition."""
    sweep_mod._previous_ok = True
    try:
        monkeypatch.setattr(sweep_mod, "run_health_checks", lambda: _results())
        monkeypatch.setattr(sweep_mod, "_persist_run", lambda results: "HALT")
        monkeypatch.setattr(
            sweep_mod, "evaluate_settlement_health",
            lambda: _decision(False, reason=HALT_ABSTAIN),
        )
        seen = []
        monkeypatch.setattr(
            sweep_mod, "_maybe_alert_and_log",
            lambda previous_ok, decision: seen.append(previous_ok),
        )

        sweep_mod.run_sweep()
        sweep_mod.run_sweep()

        # First tick sees the pre-halt state (True); second tick sees the
        # now-halted state (False) — proves _previous_ok persists correctly.
        assert seen == [True, False]
    finally:
        sweep_mod._previous_ok = True
