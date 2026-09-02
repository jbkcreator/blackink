"""Application layer of the three-layer cold-SMS block (Week 1 Subtask
1.2.3): a pre-dispatch linter that blocks a cold SMS before any provider
API call is made.

Reuses campaign_readiness_gate.is_engaged() for the "is this contact
allowed SMS" predicate rather than redefining it — same literal rule from
the master blueprint §3.0.4 (inbound_sms_count > 0 OR booked_appointment_id
IS NOT NULL), enforced here in the actual send path and, independently,
by sms_dispatch_log's CHECK constraint at the DB layer
(migrations/apply_sms_dispatch_log.py) and by this module's own tests
running in the required CI suite (tests/test_tenant_isolation.py).

No SMS vendor is contracted yet — SmsProvider/StubSmsProvider mirrors
DncProvider/StubDncProvider from compliance_gate.py.

Does not commit — caller's session_scope() owns the transaction.
"""

from abc import ABC, abstractmethod
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from src.services.campaign_readiness_gate import is_engaged


class ColdSMSBlockedError(RuntimeError):
	"""Raised when dispatch_sms() is called for a contact with no prior
	inbound SMS and no booked appointment. Non-recoverable by design — the
	caller must not retry, only re-evaluate the contact's engagement."""


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


def _record_cold_sms_blocked(session: Session, contact_id: int, client_id: str, layer: str) -> None:
	session.execute(
		text(
			"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
			"VALUES (:client_id, 'cold_sms_blocked', 'contact', :entity_id, "
			"jsonb_build_object('layer', :layer))"
		),
		{"client_id": client_id, "entity_id": str(contact_id), "layer": layer},
	)


def dispatch_sms(
	session: Session,
	contact_id: int,
	client_id: str,
	message: str,
	sms_provider: Optional[SmsProvider] = None,
) -> str:
	"""Application-layer enforcement: blocks a cold contact before
	sms_provider is ever touched. Returns the provider's message id on
	success; raises ColdSMSBlockedError (and logs cold_sms_blocked with
	layer='APPLICATION') for a cold contact."""
	sms_provider = sms_provider or StubSmsProvider()

	contact = session.execute(
		text(
			"SELECT phone, inbound_sms_count, booked_appointment_id "
			"FROM contacts WHERE contact_id = :contact_id"
		),
		{"contact_id": contact_id},
	).one()

	if not is_engaged(contact.inbound_sms_count, contact.booked_appointment_id):
		_record_cold_sms_blocked(session, contact_id, client_id, layer="APPLICATION")
		raise ColdSMSBlockedError(
			f"contact {contact_id} is cold (inbound_sms_count=0, no booked_appointment_id) — "
			"SMS dispatch blocked before any provider call"
		)

	message_id = sms_provider.send(contact.phone, message)

	session.execute(
		text(
			"INSERT INTO sms_dispatch_log "
			"(client_id, contact_id, inbound_sms_count_at_send, booked_appointment_id_at_send, "
			"status, provider_message_id) "
			"VALUES (:client_id, :contact_id, :inbound_sms_count, :booked_appointment_id, "
			"'SENT', :provider_message_id)"
		),
		{
			"client_id": client_id,
			"contact_id": contact_id,
			"inbound_sms_count": contact.inbound_sms_count,
			"booked_appointment_id": contact.booked_appointment_id,
			"provider_message_id": message_id,
		},
	)
	return message_id
