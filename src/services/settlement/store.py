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

That BLOCKED is no longer terminal: charge.py records
EVIDENCE_PACKET_UNPUBLISHED as a *retryable* blocked reason with a
next_retry_at, so a transient Stripe Files outage self-heals on a later
sweep tick instead of forfeiting the settlement (PR #30 review finding 1
— the claim query used to exclude every BLOCKED row forever). See
src/services/settlement/ledger.py's _RETRYABLE_BLOCKED_REASONS.
"""
from __future__ import annotations

import io
import logging
from abc import ABC, abstractmethod
from typing import Optional

logger = logging.getLogger(__name__)

# Stripe's Files API accepts a PDF under this purpose AND it is one of the
# purposes eligible for a FileLink (business_icon, business_logo,
# customer_signature, dispute_evidence, finance_report_run, pci_document,
# tax_document_user_upload, terminal_*): both are required here, since the
# invoice needs an unauthenticated URL.
#
# Do NOT change this back to "business_logo" — that purpose accepts image
# formats only, so every PDF upload failed with an invalid-file error which
# the old code swallowed into a None, leaving every settlement charge
# permanently BLOCKED and no client ever billed (PR #30 review finding 1).
# "dispute_evidence" is also the semantically honest choice: this packet is
# precisely the evidence we would submit if a client disputed the charge.
_EVIDENCE_FILE_PURPOSE = "dispute_evidence"


class EvidencePacketPublishError(RuntimeError):
	"""A genuine transport/API failure while publishing — the caller should
	record the message and retry later. Distinct from a publish() returning
	None, which means "no store is configured" (fail closed, never retried
	into a charge)."""


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
	"""Uploads the packet via the Stripe Files API with a FileLink created in
	the same call, and returns the FileLink's unauthenticated url.

	The link is requested atomically via `file_link_data` rather than as a
	second file_links.create call: a failure between two calls would leave an
	uploaded file in Stripe that no settlement row references, and the retry
	would upload another one."""

	def __init__(self) -> None:
		from src.services.payment_auth import _stripe_client

		self._client = _stripe_client()

	def publish(self, *, transaction_id: int, installment: int, pdf_bytes: bytes) -> Optional[str]:
		import stripe

		# A real filename matters: this is what an operator sees in the
		# Stripe dashboard when verifying the packet behind a charge.
		filename = f"evidence-packet-{transaction_id}-installment-{installment}.pdf"
		buffer = io.BytesIO(pdf_bytes)
		buffer.name = filename

		try:
			file_obj = self._client.files.create(
				params={
					"purpose": _EVIDENCE_FILE_PURPOSE,
					"file": buffer,
					"file_link_data": {"create": True},
				},
			)
		except stripe.StripeError as exc:
			logger.exception(
				"settlement.store: Stripe Files upload failed (transaction=%s installment=%s "
				"purpose=%s code=%s): %s",
				transaction_id, installment, _EVIDENCE_FILE_PURPOSE,
				getattr(exc, "code", None), exc,
			)
			raise EvidencePacketPublishError(
				f"stripe files upload failed (code={getattr(exc, 'code', None)}): {exc}"
			) from exc

		url = self._file_link_url(file_obj)
		if url is None:
			logger.error(
				"settlement.store: Stripe Files upload succeeded but no FileLink url came back "
				"(transaction=%s installment=%s file=%s)",
				transaction_id, installment, getattr(file_obj, "id", None),
			)
			raise EvidencePacketPublishError("stripe files upload returned no file_link url")
		return url

	@staticmethod
	def _file_link_url(file_obj) -> Optional[str]:
		links = getattr(file_obj, "links", None)
		data = getattr(links, "data", None) if links is not None else None
		if not data:
			return None
		return getattr(data[0], "url", None)


def get_evidence_packet_store() -> EvidencePacketStore:
	from config.settings import get_settings

	settings = get_settings()
	if settings.settlement_evidence_packet_store == "stripe_files":
		return StripeFileEvidencePacketStore()
	return StubEvidencePacketStore()
