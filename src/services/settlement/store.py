"""Evidence Packet publication (Subtask 1.2.2).

Stripe Invoices have no attachment field of their own, so Stripe Files +
FileLink is the store here — the zero-new-infrastructure option, since no
general-purpose object storage has ever been built in this repo (the same
gap that leaves contacts.ovs_pdf_url unpopulated).

publish() returning None is a FAIL-CLOSED signal, not a "proceed anyway"
one: src/services/settlement/charge.py treats a None the same as a
publish() exception — the row is marked BLOCKED /
EVIDENCE_PACKET_UNPUBLISHED and no Stripe invoice is created. With
StubEvidencePacketStore configured (the default — see
config/settings.py's settlement_evidence_packet_store), the pipeline
compiles packets and charges nothing, same posture as
EMAIL_SENDING_ENABLED / OVS_PDF_ALLOWED_HOSTS.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class EvidencePacketStore(ABC):
	@abstractmethod
	def publish(self, *, transaction_id: int, installment: int, pdf_bytes: bytes) -> Optional[str]:
		"""Return a durable URL, or None if no store is configured / the
		publish attempt failed. Never raise for an ordinary "not configured"
		condition — reserve exceptions for a genuine transport failure the
		caller should retry."""


class StubEvidencePacketStore(EvidencePacketStore):
	"""No object-storage integration exists in this repo. Always returns
	None — every charge stays BLOCKED until a real store is configured."""

	def publish(self, *, transaction_id: int, installment: int, pdf_bytes: bytes) -> Optional[str]:
		return None


class StripeFileEvidencePacketStore(EvidencePacketStore):
	"""Uploads via the Stripe Files API and creates a FileLink, then returns
	the FileLink's public url. The exact `purpose` value Stripe accepts for
	an arbitrary PDF, and FileLink's availability/expiry behavior, must be
	verified against Stripe's live test-mode API during implementation —
	not assumed (same posture booking_link.py takes with
	_GHL_PREFILL_PARAMS)."""

	def __init__(self) -> None:
		from src.services.payment_auth import _stripe_client

		self._client = _stripe_client()

	def publish(self, *, transaction_id: int, installment: int, pdf_bytes: bytes) -> Optional[str]:
		import io

		try:
			file_obj = self._client.files.create(
				params={
					"purpose": "business_logo",  # placeholder — verify the correct
					# purpose value for an arbitrary PDF against Stripe's live API
					# before relying on this in production.
					"file": io.BytesIO(pdf_bytes),
				},
			)
			link = self._client.file_links.create(params={"file": file_obj.id})
			return link.url
		except Exception:
			return None


def get_evidence_packet_store() -> EvidencePacketStore:
	from config.settings import get_settings

	settings = get_settings()
	if settings.settlement_evidence_packet_store == "stripe_files":
		return StripeFileEvidencePacketStore()
	return StubEvidencePacketStore()
