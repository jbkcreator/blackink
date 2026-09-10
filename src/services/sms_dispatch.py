"""Application layer of the three-layer cold-SMS block (Week 1 Subtask
1.2.3): a pre-dispatch linter that blocks a non-SMS-eligible contact before
any provider API call is made, and a transactional-outbox dispatch record
so a crash or DB failure around the provider call can never cause a silent
duplicate send or a missing audit trail.

Requires a FRESH evaluate_full_readiness() call (re-running Checks 1-4,
not just the cold/engaged predicate) immediately before dispatch and
proceeds only when it yields TRANSACTIONAL_SMS_ONLY — an opted-out,
DNC-listed, non-poach-suppressed, quiet-hours, or no-usable-phone contact
must never reach the provider just because inbound_sms_count/
booked_appointment_id alone looked "engaged" (PR #10 review fixup;
previously this module only checked is_engaged(), which is necessary but
not sufficient — engaged and compliant are different questions).

The dispatch record itself follows a transactional-outbox pattern rather
than a single INSERT after the provider call returns: a PENDING row with a
unique idempotency_key is committed, in its own independent transaction,
BEFORE sms_provider.send() is ever invoked. That row is durable regardless
of what happens next — if the provider call, the process, or the DB
connection dies before the terminal SENT/FAILED update lands, the PENDING
row (with its idempotency_key) is exactly the record a reconciliation job
needs to find and resolve the uncertain outcome, instead of a naive retry
blindly re-sending. Independent transactions are required, not just
statement ordering within one transaction — a single caller-owned
transaction that rolls back after the provider call would take the
"PENDING" insert down with it, which defeats the whole point (PR #10
review fixup). Same reasoning applies to the cold_sms_blocked audit event:
it's written in its own committed transaction so it survives
ColdSMSBlockedError propagating out of and rolling back the caller's
session_scope() (a prior version wrote it in the caller's own session,
where the standard except-rollback-raise in src/core/database.py's
session_scope() silently discarded it on the normal, undecorated call
path).

Reuses campaign_readiness_gate.evaluate_full_readiness()/is_engaged()
rather than redefining the eligibility rule — same literal predicates from
the master blueprint, enforced here in the actual send path, independently
by sms_dispatch_log's CHECK constraint at the DB layer
(migrations/apply_sms_dispatch_log.py), and by this module's own tests
(tests/test_sms_dispatch.py, pure-unit; tests/test_tenant_isolation.py,
live-DB), both required by .github/workflows/tests.yml on every PR — the
literal "CI/CD build test fails" layer from the master blueprint §3.0.4.

A prior version of this docstring named test_tenant_isolation.py alone as
"the required CI suite" — true for the PR that introduced it (that PR also
touched apply_sms_dispatch_log.py, which triggers
tenant_leakage_nightly.yml's path-filtered leakage-suite job, matching
Subtask 1.2.3's own DoD sign-off), but not a durable guarantee: that
workflow's path filter is scoped to models.py/migrations/database.py/
tenant_policies.py, not src/services/**, so a later PR touching only this
module's own logic would never have triggered it. tests.yml (no path
filter, every PR) closes that gap; test_sms_dispatch.py was never wired
into any workflow at all before it existed.

No SMS vendor is contracted yet — SmsProvider/StubSmsProvider mirrors
DncProvider/StubDncProvider from compliance_gate.py.

Idempotency (PR #10 2nd review fixup): a bare per-call random
idempotency_key only proved the PENDING row *exists* before the provider
call — it did nothing to stop a *retried* dispatch_sms() call (after a
crash, a commit failure, or a caller-side timeout) from generating a new
key and sending a second, duplicate SMS. Callers that may retry a given
logical send MUST pass the SAME `idempotency_key` on every attempt for
that send. With a key supplied, dispatch_sms() looks up any existing
sms_dispatch_log row for (client_id, idempotency_key) — scoped to the
client by the table's UNIQUE(client_id, idempotency_key) constraint —
before doing anything else:
  * SENT   -> returns the already-recorded provider_message_id; the
             provider is never called again.
  * PENDING/UNKNOWN -> raises SmsDispatchNeedsReconciliationError. The
             prior attempt's outcome is not known (did the provider
             accept it or not?), so retrying blindly could double-send;
             a reconciliation worker (querying the provider's own
             delivery status by idempotency_key, not built in this
             subtask — no vendor is contracted) must resolve the row to
             SENT/FAILED before another attempt is allowed.
  * FAILED -> the only status that means "definitely not sent" (nothing
             currently produces it, but a future provider integration
             that can distinguish "rejected before send" from "unknown"
             would use it); safe to retry.
  * no row -> proceeds exactly as a fresh dispatch, using this key for
             the new PENDING insert, and also passes it to
             sms_provider.send() so a provider with its own native
             idempotency-key support (most SMS/carrier APIs have one)
             gets the same guarantee independently.
A caller that omits `idempotency_key` gets a random one generated
per-call, same as before — that remains correct for genuine one-shot
sends, but such a call has no retry protection at all.

A provider-call exception now marks the row UNKNOWN, not FAILED: once
sms_provider.send() has been invoked, an exception (timeout, connection
drop, 5xx) does not tell us whether the carrier actually accepted the
message before failing — treating that as FAILED (implying "safe to
retry") is exactly the bug this fixup closes. UNKNOWN correctly routes
a retry attempt into the reconciliation-required path above instead of
a silent second send.

Transactional contract: the caller-supplied `session` is used only for the
readiness re-evaluation (same "does not commit — caller's session_scope()
owns the transaction" convention evaluate_full_readiness already has
elsewhere). The outbox writes (PENDING insert, terminal SENT/FAILED
update, and the blocked-audit event) each open and commit their own
independent session — by default a fresh get_db_context(client_id=...),
overridable via `open_outbox_session` for testing without a live DB.
"""

import uuid
from abc import ABC, abstractmethod
from typing import Callable, ContextManager, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.core.database import get_db_context
from src.services.campaign_readiness_gate import evaluate_full_readiness
from src.services.cold_sms_gate import get_booked_appointment_id, get_inbound_sms_count
from src.services.compliance_gate import DncProvider

_ELIGIBLE = "TRANSACTIONAL_SMS_ONLY"


class ColdSMSBlockedError(RuntimeError):
    """Raised when dispatch_sms() is called for a contact that isn't
    SMS-eligible right now — cold (no prior inbound SMS, no booked
    appointment), opted out, non-poach suppressed, DNC-listed, in quiet
    hours, or with a DNC result that couldn't be determined. Non-recoverable
    by design — the caller must not retry, only re-evaluate the contact."""


class SmsDispatchNeedsReconciliationError(RuntimeError):
    """Raised when dispatch_sms() is called with an idempotency_key whose
    prior attempt is still PENDING or UNKNOWN — its outcome isn't known, so
    another attempt could double-send. Non-recoverable by design: the
    caller must not retry with this key until a reconciliation job has
    resolved the existing row to SENT or FAILED."""


class SmsProvider(ABC):
    @abstractmethod
    def send(self, phone: str, message: str, idempotency_key: str) -> str:
        """Send the SMS and return the provider's message id. Implementations
        that talk to a carrier with native idempotency-key support should
        pass it through, for a second guarantee independent of this
        module's own outbox dedup."""


class StubSmsProvider(SmsProvider):
    """No vendor contracted yet — same posture as StubDncProvider. Any real
    call means procurement hasn't landed; fail loudly rather than pretend
    to send."""

    def send(self, phone: str, message: str, idempotency_key: str) -> str:
        raise NotImplementedError("no SMS vendor contracted yet")


OutboxSessionFactory = Callable[[], ContextManager[Session]]


def _record_cold_sms_blocked(
    session: Session, contact_id: int, client_id: str, layer: str, reason_code: str
) -> None:
    session.execute(
        text(
            "INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
            "VALUES (:client_id, 'cold_sms_blocked', 'contact', :entity_id, "
            "jsonb_build_object('layer', :layer, 'reason_code', :reason_code))"
        ),
        {"client_id": client_id, "entity_id": str(contact_id), "layer": layer, "reason_code": reason_code},
    )


def _existing_dispatch(session: Session, client_id: str, idempotency_key: str):
    return session.execute(
        text(
            "SELECT status, provider_message_id FROM sms_dispatch_log "
            "WHERE client_id = :client_id AND idempotency_key = :idempotency_key"
        ),
        {"client_id": client_id, "idempotency_key": idempotency_key},
    ).one_or_none()


def dispatch_sms(
    session: Session,
    contact_id: int,
    client_id: str,
    message: str,
    sms_provider: Optional[SmsProvider] = None,
    dnc_provider: Optional[DncProvider] = None,
    open_outbox_session: Optional[OutboxSessionFactory] = None,
    idempotency_key: Optional[str] = None,
) -> str:
    """Re-evaluates full readiness and, only if it yields
    TRANSACTIONAL_SMS_ONLY, dispatches via a transactional outbox: a
    committed PENDING sms_dispatch_log row (with a unique idempotency_key)
    before sms_provider is ever touched, then a committed SENT/FAILED/
    UNKNOWN update after. Returns the provider's message id on success;
    raises ColdSMSBlockedError (and durably logs cold_sms_blocked with
    layer='APPLICATION') for any non-SMS-eligible contact.

    Pass the SAME `idempotency_key` on every retry of a given logical send
    — dispatch_sms() then looks up any prior sms_dispatch_log row for
    (client_id, idempotency_key) first: SENT short-circuits to the
    already-recorded message id (no second provider call), PENDING/UNKNOWN
    raises SmsDispatchNeedsReconciliationError instead of risking a
    duplicate send. Omitting it generates a random one-shot key with no
    retry protection. See module docstring.

    `open_outbox_session` overrides how the independent outbox
    transactions are opened — defaults to a fresh get_db_context(client_id=
    client_id) per call; tests substitute a fake context-manager factory to
    exercise outbox-write failures without a live DB.
    """
    sms_provider = sms_provider or StubSmsProvider()
    open_outbox_session = open_outbox_session or (lambda: get_db_context(client_id=client_id))

    if idempotency_key is not None:
        existing = _existing_dispatch(session, client_id, idempotency_key)
        if existing is not None:
            if existing.status == "SENT":
                return existing.provider_message_id
            if existing.status in ("PENDING", "UNKNOWN"):
                raise SmsDispatchNeedsReconciliationError(
                    f"dispatch with idempotency_key {idempotency_key!r} is still "
                    f"{existing.status} — resolve it before retrying"
                )
            # status == 'FAILED': known never to have reached the provider
            # successfully, safe to fall through and redispatch below.

    readiness = evaluate_full_readiness(session, contact_id, client_id, dnc_provider=dnc_provider)

    if readiness.compliance_eligibility != _ELIGIBLE:
        with open_outbox_session() as audit_session:
            _record_cold_sms_blocked(
                audit_session, contact_id, client_id, layer="APPLICATION", reason_code=readiness.reason_code
            )
        raise ColdSMSBlockedError(
            f"contact {contact_id} is not SMS-eligible ({readiness.reason_code}) — "
            "SMS dispatch blocked before any provider call"
        )

    contact = session.execute(
        text("SELECT phone FROM contacts WHERE contact_id = :contact_id"),
        {"contact_id": contact_id},
    ).one()

    # D-7 fix (audit 2026-09-10), same source of truth as
    # campaign_readiness_gate.evaluate_full_readiness() above: the
    # sms_dispatch_log audit snapshot must reflect the SAME engagement
    # signal that just decided this contact was eligible, not the separate,
    # never-written contacts.inbound_sms_count/booked_appointment_id
    # columns — reading those here (as this line used to) is what caused
    # ck_sms_dispatch_log_not_cold to reject a genuinely-engaged contact's
    # own PENDING insert the moment evaluate_full_readiness() was fixed to
    # read the events ledger instead, since the two would then disagree.
    resolved_key = idempotency_key or uuid.uuid4().hex

    with open_outbox_session() as outbox_session:
        dispatch_id = outbox_session.execute(
            text(
                "INSERT INTO sms_dispatch_log "
                "(client_id, contact_id, inbound_sms_count_at_send, booked_appointment_id_at_send, "
                "status, idempotency_key) "
                "VALUES (:client_id, :contact_id, :inbound_sms_count, :booked_appointment_id, "
                "'PENDING', :idempotency_key) "
                "ON CONFLICT (client_id, idempotency_key) DO NOTHING "
                "RETURNING dispatch_id"
            ),
            {
                "client_id": client_id,
                "contact_id": contact_id,
                "inbound_sms_count": get_inbound_sms_count(outbox_session, contact_id),
                "booked_appointment_id": get_booked_appointment_id(outbox_session, contact_id),
                "idempotency_key": resolved_key,
            },
        ).scalar()

    if dispatch_id is None:
        # A concurrent call won the race to insert this same key between our
        # lookup above and this INSERT — re-check the row it created instead
        # of silently re-dispatching.
        existing = _existing_dispatch(session, client_id, resolved_key)
        if existing is not None and existing.status == "SENT":
            return existing.provider_message_id
        raise SmsDispatchNeedsReconciliationError(
            f"a concurrent dispatch with idempotency_key {resolved_key!r} is in progress or "
            "unresolved — resolve it before retrying"
        )

    try:
        message_id = sms_provider.send(contact.phone, message, idempotency_key=resolved_key)
    except Exception:
        with open_outbox_session() as outbox_session:
            outbox_session.execute(
                text("UPDATE sms_dispatch_log SET status = 'UNKNOWN' WHERE dispatch_id = :dispatch_id"),
                {"dispatch_id": dispatch_id},
            )
        raise

    with open_outbox_session() as outbox_session:
        outbox_session.execute(
            text(
                "UPDATE sms_dispatch_log SET status = 'SENT', provider_message_id = :provider_message_id "
                "WHERE dispatch_id = :dispatch_id"
            ),
            {"provider_message_id": message_id, "dispatch_id": dispatch_id},
        )

    return message_id
