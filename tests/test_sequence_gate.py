"""Tests for Task 3.1.1 — gate split and sequence enrollment.

Tests drive through PUBLIC interfaces (evaluate_touch_gate,
evaluate_enrollment_gate, may_enroll), not private helpers. No live DB.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from src.services.compliance_gate import (
    evaluate_enrollment_gate,
    evaluate_touch_gate,
)


def _contact(**overrides):
    base = dict(
        contact_id=1,
        company_id="comp_abc",
        email_status="VERIFIED",
        is_opted_out=False,
        suppression_state=False,
        compliance_eligibility="EMAIL_COLD_ELIGIBLE",
        last_outbound_touch_at=None,
        phone="+15551234567",
        # Default to a fresh cached clean result so StubDncProvider (returns None)
        # doesn't ABSTAIN on tests that aren't exercising DNC behaviour.
        dnc_clean=True,
        dnc_checked_at=datetime.now(timezone.utc) - timedelta(days=1),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _GateFakeSession:
    """Handles _record() INSERTs and _check_non_poach() SELECTs."""

    def __init__(self, non_poach_claimed=False, non_poach_error=False):
        self._claimed = non_poach_claimed
        self._error = non_poach_error
        self.recorded = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        params = params or {}
        if "INSERT INTO compliance_gate_checks" in sql:
            self.recorded.append(dict(params))
            return SimpleNamespace(rowcount=1)
        if "is_claimed_by_other_client" in sql:
            if self._error:
                raise RuntimeError("simulated db error")
            return SimpleNamespace(scalar=lambda: self._claimed)
        raise NotImplementedError(f"_GateFakeSession unhandled: {sql[:80]}")


# ── Cycle 1: touch gate ignores cooldown ────────────────────────────────


def test_touch_gate_ignores_cooldown():
    """Contact with a 3-day-old touch still passes the touch gate.
    Cooldown is an enrollment guard, not a per-touch guard."""
    contact = _contact(last_outbound_touch_at=datetime.now(timezone.utc) - timedelta(days=3))
    session = _GateFakeSession(non_poach_claimed=False)
    result = evaluate_touch_gate(session, contact, "client_a")
    assert result.ready is True


def test_touch_gate_still_blocks_opted_out_contact():
    """Deterministic columns check runs in touch gate."""
    contact = _contact(is_opted_out=True)
    session = _GateFakeSession(non_poach_claimed=False)
    result = evaluate_touch_gate(session, contact, "client_a")
    assert result.ready is False


def test_touch_gate_still_blocks_poached_company():
    """Non-poach check runs in touch gate."""
    contact = _contact()
    session = _GateFakeSession(non_poach_claimed=True)
    result = evaluate_touch_gate(session, contact, "client_a")
    assert result.ready is False


# ── Cycle 2: enrollment gate includes cooldown ───────────────────────────


def test_enrollment_gate_blocks_on_recent_cooldown():
    """Contact with a 3-day-old touch is blocked by enrollment gate."""
    contact = _contact(last_outbound_touch_at=datetime.now(timezone.utc) - timedelta(days=3))
    session = _GateFakeSession(non_poach_claimed=False)
    result = evaluate_enrollment_gate(session, contact, "client_a")
    assert result.ready is False
    assert any("cooldown" in r for r in result.blocked_reasons)


def test_enrollment_gate_passes_with_no_prior_touch():
    """Contact never touched passes enrollment gate."""
    contact = _contact()
    session = _GateFakeSession(non_poach_claimed=False)
    result = evaluate_enrollment_gate(session, contact, "client_a")
    assert result.ready is True


def test_enrollment_gate_passes_after_14_days():
    """Contact touched 15 days ago clears the 14-day cooldown."""
    contact = _contact(last_outbound_touch_at=datetime.now(timezone.utc) - timedelta(days=15))
    session = _GateFakeSession(non_poach_claimed=False)
    result = evaluate_enrollment_gate(session, contact, "client_a")
    assert result.ready is True


# ── Cycle 3: DNC no-phone → PASS (via public interface) ─────────────────


def test_touch_gate_passes_contact_with_no_phone():
    """No phone means DNC registry is not applicable — PASS, not ABSTAIN.
    Ensures a contact without a phone number is not silently blocked."""
    contact = _contact(phone=None)
    session = _GateFakeSession(non_poach_claimed=False)
    result = evaluate_touch_gate(session, contact, "client_a")
    assert result.ready is True


# ── Cycle 6: may_enroll ──────────────────────────────────────────────────


def test_may_enroll_false_when_active_run_exists(monkeypatch):
    """Returns False when sequence_runs has an ACTIVE row for this contact_id."""
    import src.services.sequence_enrollment as se

    @contextmanager
    def _fake_system_db():
        yield SimpleNamespace(
            execute=lambda *a, **kw: SimpleNamespace(scalar=lambda: 1)
        )

    monkeypatch.setattr(se, "get_system_db_context", _fake_system_db)
    assert se.may_enroll(contact_id=42) is False


def test_may_enroll_true_when_no_active_run(monkeypatch):
    """Returns True when no ACTIVE sequence_run exists for this contact_id."""
    import src.services.sequence_enrollment as se

    @contextmanager
    def _fake_system_db():
        yield SimpleNamespace(
            execute=lambda *a, **kw: SimpleNamespace(scalar=lambda: 0)
        )

    monkeypatch.setattr(se, "get_system_db_context", _fake_system_db)
    assert se.may_enroll(contact_id=42) is True


def test_may_enroll_false_when_opted_out(monkeypatch):
    """Enroll-time 'belt': a globally opted-out contact is never re-enrollable,
    even with no active run. The is_opted_out lookup is the first query."""
    import src.services.sequence_enrollment as se

    calls = {"n": 0}

    def _execute(*a, **kw):
        calls["n"] += 1
        # First execute = is_opted_out lookup → True (short-circuits).
        return SimpleNamespace(scalar=lambda: True if calls["n"] == 1 else 0)

    @contextmanager
    def _fake_system_db():
        yield SimpleNamespace(execute=_execute)

    monkeypatch.setattr(se, "get_system_db_context", _fake_system_db)
    assert se.may_enroll(contact_id=42) is False
    assert calls["n"] == 1  # never reached the active-run count


# ── enroll_contact: touch-2 dial is NOT enqueued (event-driven) ──────────────


def test_enroll_contact_skips_touch2_dial(monkeypatch):
    """enroll_contact enqueues touches 1/3/4/5 upfront but NOT the touch-2 dial
    — that is posted event-driven on Touch 1 approval (ADR 0001). Enqueuing it
    here would orphan it, since the sweep never posts DIAL_TASK."""
    import src.services.sequence_enrollment as se
    import src.services.work_orders as wo

    monkeypatch.setattr(se, "may_enroll", lambda contact_id: True)

    enqueued: list[dict] = []
    monkeypatch.setattr(wo, "enqueue", lambda **kw: enqueued.append(kw) or SimpleNamespace(action_id="a"))

    @contextmanager
    def _nested():
        yield SimpleNamespace()

    session = SimpleNamespace(
        begin_nested=_nested,
        execute=lambda *a, **kw: SimpleNamespace(),
    )

    se.enroll_contact(session, client_id="acme", contact_id=7, contact_email="p@x.com")

    steps = sorted(kw["payload"]["touch_step"] for kw in enqueued)
    classes = {kw["action_class"] for kw in enqueued}
    assert steps == [1, 3, 4, 5]
    assert "DIAL_TASK" not in classes
    assert "LINKEDIN_TASK" in classes
