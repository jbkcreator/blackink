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
running in the required CI suite (tests/test_tenant_isolation.py).

No SMS vendor is contracted yet — SmsProvider/StubSmsProvider mirrors
DncProvider/StubDncProvider from compliance_gate.py.

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
from src.services.compliance_gate import DncProvider

_ELIGIBLE = "TRANSACTIONAL_SMS_ONLY"


class ColdSMSBlockedError(RuntimeError):
    """Raised when dispatch_sms() is called for a contact that isn't
    SMS-eligible right now — cold (no prior inbound SMS, no booked
    appointment), opted out, non-poach suppressed, DNC-listed, in quiet
    hours, or with a DNC result that couldn't be determined. Non-recoverable
    by design — the caller must not retry, only re-evaluate the contact."""


class SmsProvider(ABC):
    @abstractmethod
    def send(self, phone: str, message: str) -> str:
        """Send the SMS and return the provider's message id."""


class StubSmsProvider(SmsProvider):
    """No vendor contracted yet — same posture as StubDncProvider. Any real
    call means procurement hasn't landed; fail loudly rather than pretend
    to send."""

    def send(self, phone: str, message: str) -> str:
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


def dispatch_sms(
    session: Session,
    contact_id: int,
    client_id: str,
    message: str,
    sms_provider: Optional[SmsProvider] = None,
    dnc_provider: Optional[DncProvider] = None,
    open_outbox_session: Optional[OutboxSessionFactory] = None,
) -> str:
    """Re-evaluates full readiness and, only if it yields
    TRANSACTIONAL_SMS_ONLY, dispatches via a transactional outbox: a
    committed PENDING sms_dispatch_log row (with a unique idempotency_key)
    before sms_provider is ever touched, then a committed SENT/FAILED
    update after. Returns the provider's message id on success; raises
    ColdSMSBlockedError (and durably logs cold_sms_blocked with
    layer='APPLICATION') for any non-SMS-eligible contact.

    `open_outbox_session` overrides how the independent outbox
    transactions are opened — defaults to a fresh get_db_context(client_id=
    client_id) per call; tests substitute a fake context-manager factory to
    exercise outbox-write failures without a live DB.
    """
    sms_provider = sms_provider or StubSmsProvider()
    open_outbox_session = open_outbox_session or (lambda: get_db_context(client_id=client_id))

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
        text(
            "SELECT phone, inbound_sms_count, booked_appointment_id "
            "FROM contacts WHERE contact_id = :contact_id"
        ),
        {"contact_id": contact_id},
    ).one()

    idempotency_key = uuid.uuid4().hex

    with open_outbox_session() as outbox_session:
        dispatch_id = outbox_session.execute(
            text(
                "INSERT INTO sms_dispatch_log "
                "(client_id, contact_id, inbound_sms_count_at_send, booked_appointment_id_at_send, "
                "status, idempotency_key) "
                "VALUES (:client_id, :contact_id, :inbound_sms_count, :booked_appointment_id, "
                "'PENDING', :idempotency_key) "
                "RETURNING dispatch_id"
            ),
            {
                "client_id": client_id,
                "contact_id": contact_id,
                "inbound_sms_count": contact.inbound_sms_count,
                "booked_appointment_id": contact.booked_appointment_id,
                "idempotency_key": idempotency_key,
            },
        ).scalar()

    try:
        message_id = sms_provider.send(contact.phone, message)
    except Exception:
        with open_outbox_session() as outbox_session:
            outbox_session.execute(
                text("UPDATE sms_dispatch_log SET status = 'FAILED' WHERE dispatch_id = :dispatch_id"),
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
