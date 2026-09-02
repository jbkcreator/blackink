"""Pure unit tests (no live DB) for Week 1 Subtask 1.2.3's application-layer
SMS dispatch: the fresh-readiness-check linter and the transactional-outbox
write pattern (PR #10 review fixup). Mirrors test_campaign_readiness_gate.py's
FakeSession pattern.

evaluate_full_readiness() itself is monkeypatched rather than simulated
through a FakeSession — it already has its own thorough coverage in
test_campaign_readiness_gate.py and (live-DB) test_tenant_isolation.py;
what these tests need to control is only its *result*, to prove
dispatch_sms() proceeds on TRANSACTIONAL_SMS_ONLY and blocks on everything
else. The outbox writes (PENDING insert, terminal SENT/FAILED update, the
blocked-audit event) go through dispatch_sms()'s injectable
`open_outbox_session` factory, which is exactly the seam production code
uses too (it defaults to a fresh get_db_context(client_id=...) per call).
The DB-layer CHECK constraint and the full dispatch_sms() path against a
real Postgres are covered separately in tests/test_tenant_isolation.py.
"""

from types import SimpleNamespace

import pytest

import src.services.sms_dispatch as sms_dispatch_module
from src.services.campaign_readiness_gate import FullReadinessResult
from src.services.sms_dispatch import ColdSMSBlockedError, SmsProvider, dispatch_sms


class _CountingSmsProvider(SmsProvider):
    def __init__(self, raise_error=None):
        self.calls = []
        self._raise_error = raise_error

    def send(self, phone, message):
        self.calls.append((phone, message))
        if self._raise_error:
            raise self._raise_error
        return "stub-message-id"


class _FakeResult:
    def __init__(self, row=None, scalar=None):
        self._row = row
        self._scalar = scalar

    def one(self):
        return self._row

    def scalar(self):
        return self._scalar


class _FakeSession:
    """The caller-supplied `session` — dispatch_sms only touches it for the
    post-readiness SELECT phone/inbound_sms_count/booked_appointment_id,
    since evaluate_full_readiness() itself is monkeypatched below."""

    def __init__(self, contact_row):
        self._contact_row = contact_row
        self.executed = []

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakeResult(row=self._contact_row)


class _FakeOutboxSession:
    def __init__(self, insert_returns=1):
        self._insert_returns = insert_returns
        self.executed = []

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakeResult(scalar=self._insert_returns)


class _FakeOutboxCM:
    """A `with open_outbox_session() as session:` stand-in. raise_on_exit
    simulates that transaction's commit failing after its statement(s)
    already ran — the statement was attempted, but the write never landed,
    same as a real commit failure."""

    def __init__(self, session, raise_on_exit=None):
        self.session = session
        self._raise_on_exit = raise_on_exit

    def __enter__(self):
        return self.session

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None and self._raise_on_exit is not None:
            raise self._raise_on_exit
        return False


class _ScriptedOutboxFactory:
    """Returns one scripted _FakeOutboxCM per call, in order — call N
    corresponds to the Nth `with open_outbox_session() as ...` dispatch_sms
    opens (PENDING insert, then terminal update; or, for a blocked contact,
    just the single blocked-audit-event write)."""

    def __init__(self, cms):
        self._cms = list(cms)
        self.call_count = 0

    def __call__(self):
        self.call_count += 1
        return self._cms.pop(0)


def _engaged_contact(phone="+15551234567", inbound_sms_count=1, booked_appointment_id=None):
    return SimpleNamespace(
        phone=phone, inbound_sms_count=inbound_sms_count, booked_appointment_id=booked_appointment_id
    )


def _eligible_result(contact_id=1):
    return FullReadinessResult(contact_id, True, "TRANSACTIONAL_SMS_ONLY", "ENGAGED_SMS_ELIGIBLE")


def _blocked_result(contact_id, eligibility, reason_code):
    readiness = eligibility == "TRANSACTIONAL_SMS_ONLY"
    return FullReadinessResult(contact_id, readiness, eligibility, reason_code)


# ── Finding 1: dispatch requires a fresh, fully-compliant readiness result ──


@pytest.mark.parametrize(
    "eligibility,reason_code",
    [
        ("BLOCKED", "OPT_OUT"),
        ("BLOCKED", "NON_POACH"),
        ("EMAIL_COLD_ELIGIBLE", "ENGAGED_DNC_SMS_WITHHELD"),
        ("EMAIL_COLD_ELIGIBLE", "ENGAGED_DNC_UNKNOWN_SMS_WITHHELD"),
        ("EMAIL_COLD_ELIGIBLE", "ENGAGED_QUIET_HOURS_SMS_WITHHELD"),
        ("EMAIL_COLD_ELIGIBLE", "COLD_EMAIL_ONLY"),
    ],
)
def test_non_sms_eligible_contact_raises_and_never_calls_provider(monkeypatch, eligibility, reason_code):
    """Opted-out, non-poach-suppressed, DNC-listed, DNC-unknown, quiet-hours,
    and cold contacts must all be blocked before the provider is ever
    touched — not just the plain cold/engaged case a prior version checked."""
    monkeypatch.setattr(
        sms_dispatch_module,
        "evaluate_full_readiness",
        lambda session, contact_id, client_id, dnc_provider=None: _blocked_result(
            contact_id, eligibility, reason_code
        ),
    )
    session = _FakeSession(_engaged_contact())
    provider = _CountingSmsProvider()
    audit_session = _FakeOutboxSession()
    outbox = _ScriptedOutboxFactory([_FakeOutboxCM(audit_session)])

    with pytest.raises(ColdSMSBlockedError):
        dispatch_sms(
            session,
            contact_id=1,
            client_id="client_a",
            message="hi",
            sms_provider=provider,
            open_outbox_session=outbox,
        )

    assert provider.calls == []
    assert any("cold_sms_blocked" in stmt for stmt, _ in audit_session.executed)


def test_missing_phone_contact_never_reaches_transactional_sms(monkeypatch):
    """A contact with no phone can't be TRANSACTIONAL_SMS_ONLY — the gate
    itself (evaluate_full_readiness -> _in_quiet_hours fails closed with no
    phone) already guarantees this; dispatch_sms must honor that result."""
    monkeypatch.setattr(
        sms_dispatch_module,
        "evaluate_full_readiness",
        lambda session, contact_id, client_id, dnc_provider=None: _blocked_result(
            contact_id, "EMAIL_COLD_ELIGIBLE", "ENGAGED_QUIET_HOURS_SMS_WITHHELD"
        ),
    )
    session = _FakeSession(_engaged_contact(phone=None))
    provider = _CountingSmsProvider()
    outbox = _ScriptedOutboxFactory([_FakeOutboxCM(_FakeOutboxSession())])

    with pytest.raises(ColdSMSBlockedError):
        dispatch_sms(
            session,
            contact_id=1,
            client_id="client_a",
            message="hi",
            sms_provider=provider,
            open_outbox_session=outbox,
        )
    assert provider.calls == []


def test_blocked_audit_event_carries_application_layer_and_reason_code(monkeypatch):
    monkeypatch.setattr(
        sms_dispatch_module,
        "evaluate_full_readiness",
        lambda session, contact_id, client_id, dnc_provider=None: _blocked_result(
            contact_id, "EMAIL_COLD_ELIGIBLE", "ENGAGED_DNC_SMS_WITHHELD"
        ),
    )
    session = _FakeSession(_engaged_contact())
    audit_session = _FakeOutboxSession()
    outbox = _ScriptedOutboxFactory([_FakeOutboxCM(audit_session)])

    with pytest.raises(ColdSMSBlockedError):
        dispatch_sms(
            session, contact_id=1, client_id="client_a", message="hi", open_outbox_session=outbox
        )

    insert_stmt, insert_params = next(
        (stmt, params) for stmt, params in audit_session.executed if "cold_sms_blocked" in stmt
    )
    assert insert_params["layer"] == "APPLICATION"
    assert insert_params["reason_code"] == "ENGAGED_DNC_SMS_WITHHELD"


def test_blocked_audit_event_is_committed_in_its_own_transaction_not_the_callers(monkeypatch):
    """Regression for the finding that this event was written into the
    caller's own (uncommitted) session and lost when ColdSMSBlockedError
    propagated out of get_db_context() and triggered its except-rollback.
    The audit write must go through open_outbox_session — its own,
    independently committed transaction — never the caller-supplied
    `session` at all."""
    monkeypatch.setattr(
        sms_dispatch_module,
        "evaluate_full_readiness",
        lambda session, contact_id, client_id, dnc_provider=None: _blocked_result(
            contact_id, "BLOCKED", "OPT_OUT"
        ),
    )
    caller_session = _FakeSession(_engaged_contact())
    audit_session = _FakeOutboxSession()
    outbox = _ScriptedOutboxFactory([_FakeOutboxCM(audit_session)])

    with pytest.raises(ColdSMSBlockedError):
        dispatch_sms(
            caller_session, contact_id=1, client_id="client_a", message="hi", open_outbox_session=outbox
        )

    assert not any("cold_sms_blocked" in stmt for stmt, _ in caller_session.executed), (
        "blocked-audit event must not be written into the caller's own session"
    )
    assert any("cold_sms_blocked" in stmt for stmt, _ in audit_session.executed)


# ── Finding 2: transactional outbox — durable PENDING before the provider ──


def test_pending_row_committed_before_provider_is_called(monkeypatch):
    """The PENDING outbox row's transaction must be opened AND EXITED
    (i.e. committed) before sms_provider.send() runs at all — proven here
    by a shared order-tracking list both the outbox CM and the provider
    append to."""
    monkeypatch.setattr(
        sms_dispatch_module, "evaluate_full_readiness", lambda *a, **k: _eligible_result()
    )
    order = []

    class _OrderTrackingCM(_FakeOutboxCM):
        def __exit__(self, exc_type, exc, tb):
            order.append("outbox_commit")
            return super().__exit__(exc_type, exc, tb)

    class _OrderTrackingProvider(_CountingSmsProvider):
        def send(self, phone, message):
            order.append("provider_send")
            return super().send(phone, message)

    session = _FakeSession(_engaged_contact())
    provider = _OrderTrackingProvider()
    outbox = _ScriptedOutboxFactory(
        [_OrderTrackingCM(_FakeOutboxSession(insert_returns=42)), _FakeOutboxCM(_FakeOutboxSession())]
    )

    dispatch_sms(
        session, contact_id=1, client_id="client_a", message="hi", sms_provider=provider, open_outbox_session=outbox
    )

    assert order == ["outbox_commit", "provider_send"], (
        "the PENDING dispatch row must be committed before the provider is called"
    )


def test_pending_row_carries_a_unique_idempotency_key(monkeypatch):
    monkeypatch.setattr(
        sms_dispatch_module, "evaluate_full_readiness", lambda *a, **k: _eligible_result()
    )
    session = _FakeSession(_engaged_contact())
    pending_session = _FakeOutboxSession(insert_returns=7)
    outbox = _ScriptedOutboxFactory([_FakeOutboxCM(pending_session), _FakeOutboxCM(_FakeOutboxSession())])

    dispatch_sms(
        session,
        contact_id=1,
        client_id="client_a",
        message="hi",
        sms_provider=_CountingSmsProvider(),
        open_outbox_session=outbox,
    )

    insert_stmt, insert_params = next(
        (stmt, params) for stmt, params in pending_session.executed if "INSERT INTO sms_dispatch_log" in stmt
    )
    assert "'PENDING'" in insert_stmt
    assert insert_params["idempotency_key"]
    assert len(insert_params["idempotency_key"]) > 16


def test_log_insert_failure_prevents_any_provider_call(monkeypatch):
    """If the PENDING row's own transaction fails to commit, the provider
    must never be invoked — no attempt was ever durably recorded, so no
    send should happen either."""
    monkeypatch.setattr(
        sms_dispatch_module, "evaluate_full_readiness", lambda *a, **k: _eligible_result()
    )
    session = _FakeSession(_engaged_contact())
    provider = _CountingSmsProvider()
    outbox = _ScriptedOutboxFactory(
        [_FakeOutboxCM(_FakeOutboxSession(), raise_on_exit=RuntimeError("simulated commit failure"))]
    )

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        dispatch_sms(
            session, contact_id=1, client_id="client_a", message="hi", sms_provider=provider, open_outbox_session=outbox
        )

    assert provider.calls == [], "provider must never be called if the PENDING outbox write didn't commit"


def test_commit_failure_after_provider_acceptance_propagates_without_retry(monkeypatch):
    """The provider already accepted the message when the terminal
    SENT-status commit fails — dispatch_sms must not swallow that failure
    or attempt to resend; it propagates so the caller/monitoring knows the
    outcome is uncertain and the PENDING row (committed earlier, separately)
    needs reconciliation, not a blind retry."""
    monkeypatch.setattr(
        sms_dispatch_module, "evaluate_full_readiness", lambda *a, **k: _eligible_result()
    )
    session = _FakeSession(_engaged_contact())
    provider = _CountingSmsProvider()
    terminal_session = _FakeOutboxSession()
    outbox = _ScriptedOutboxFactory(
        [
            _FakeOutboxCM(_FakeOutboxSession(insert_returns=99)),
            _FakeOutboxCM(terminal_session, raise_on_exit=RuntimeError("simulated commit failure")),
        ]
    )

    with pytest.raises(RuntimeError, match="simulated commit failure"):
        dispatch_sms(
            session, contact_id=1, client_id="client_a", message="hi", sms_provider=provider, open_outbox_session=outbox
        )

    assert len(provider.calls) == 1, "provider must be called exactly once — no retry after the commit failure"
    assert any("SET" in stmt and "SENT" in stmt for stmt, _ in terminal_session.executed), (
        "the terminal update must still have been attempted even though its commit failed"
    )


def test_provider_failure_marks_dispatch_failed_and_reraises(monkeypatch):
    monkeypatch.setattr(
        sms_dispatch_module, "evaluate_full_readiness", lambda *a, **k: _eligible_result()
    )
    session = _FakeSession(_engaged_contact())
    provider = _CountingSmsProvider(raise_error=ConnectionError("provider unreachable"))
    failed_session = _FakeOutboxSession()
    outbox = _ScriptedOutboxFactory(
        [_FakeOutboxCM(_FakeOutboxSession(insert_returns=5)), _FakeOutboxCM(failed_session)]
    )

    with pytest.raises(ConnectionError, match="provider unreachable"):
        dispatch_sms(
            session, contact_id=1, client_id="client_a", message="hi", sms_provider=provider, open_outbox_session=outbox
        )

    assert any("'FAILED'" in stmt for stmt, _ in failed_session.executed)


def test_engaged_and_compliant_contact_dispatches_and_writes_sent_status(monkeypatch):
    monkeypatch.setattr(
        sms_dispatch_module, "evaluate_full_readiness", lambda *a, **k: _eligible_result()
    )
    session = _FakeSession(_engaged_contact(phone="+15551234567"))
    provider = _CountingSmsProvider()
    sent_session = _FakeOutboxSession()
    outbox = _ScriptedOutboxFactory(
        [_FakeOutboxCM(_FakeOutboxSession(insert_returns=1)), _FakeOutboxCM(sent_session)]
    )

    message_id = dispatch_sms(
        session, contact_id=2, client_id="client_a", message="reminder", sms_provider=provider, open_outbox_session=outbox
    )

    assert message_id == "stub-message-id"
    assert provider.calls == [("+15551234567", "reminder")]
    update_stmt, update_params = next(
        (stmt, params) for stmt, params in sent_session.executed if "SENT" in stmt
    )
    assert update_params["provider_message_id"] == "stub-message-id"
    assert update_params["dispatch_id"] == 1
