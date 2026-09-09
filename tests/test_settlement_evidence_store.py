"""Regression tests for PR #30 review finding 1: StripeFileEvidencePacketStore
uploaded every packet with purpose="business_logo", which Stripe accepts for
images only — every real upload failed, silently converted to None, and every
settlement charge was permanently BLOCKED. No live Stripe call here; a fake
client stands in for stripe.Client, matching this repo's compliance-gate/
gateway-subclassing convention (never monkeypatching the stripe module)."""
from types import SimpleNamespace

import pytest

from src.services.settlement.store import (
	EvidencePacketPublishError,
	StripeFileEvidencePacketStore,
	_EVIDENCE_FILE_PURPOSE,
)


class _FakeStripeError(Exception):
	code = "invalid_request_error"


def _make_store(fake_files, monkeypatch):
	store = StripeFileEvidencePacketStore.__new__(StripeFileEvidencePacketStore)
	store._client = SimpleNamespace(files=fake_files)
	return store


class _FakeFilesSuccess:
	def __init__(self):
		self.calls = []

	def create(self, params):
		self.calls.append(params)
		link = SimpleNamespace(url="https://files.stripe.com/links/fake123")
		return SimpleNamespace(id="file_123", links=SimpleNamespace(data=[link]))


class _FakeFilesNoLink:
	def create(self, params):
		return SimpleNamespace(id="file_123", links=SimpleNamespace(data=[]))


class _FakeFilesRaises:
	def create(self, params):
		import stripe

		raise stripe.StripeError("invalid file for purpose")


def test_publish_uses_dispute_evidence_purpose_and_atomic_file_link(monkeypatch):
	"""Regression guard: must never revert to business_logo (image-only)."""
	assert _EVIDENCE_FILE_PURPOSE == "dispute_evidence"

	fake_files = _FakeFilesSuccess()
	store = _make_store(fake_files, monkeypatch)

	url = store.publish(transaction_id=1, installment=1, pdf_bytes=b"%PDF-fake")

	assert url == "https://files.stripe.com/links/fake123"
	assert len(fake_files.calls) == 1
	params = fake_files.calls[0]
	assert params["purpose"] == "dispute_evidence"
	assert params["file_link_data"] == {"create": True}


def test_publish_raises_on_stripe_error_instead_of_swallowing_to_none(monkeypatch):
	"""The pre-fix behavior caught Exception broadly and returned None,
	making a real Stripe failure indistinguishable from "not configured" and
	invisible in instN_last_error."""
	import stripe

	monkeypatch.setattr(stripe, "StripeError", stripe.StripeError, raising=False)
	fake_files = _FakeFilesRaises()
	store = _make_store(fake_files, monkeypatch)

	with pytest.raises(EvidencePacketPublishError, match="invalid file for purpose"):
		store.publish(transaction_id=1, installment=1, pdf_bytes=b"%PDF-fake")


def test_publish_raises_when_no_file_link_comes_back(monkeypatch):
	fake_files = _FakeFilesNoLink()
	store = _make_store(fake_files, monkeypatch)

	with pytest.raises(EvidencePacketPublishError):
		store.publish(transaction_id=1, installment=1, pdf_bytes=b"%PDF-fake")
