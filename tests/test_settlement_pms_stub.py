"""StubPmsProvider must return "couldn't determine" for everything — no
PMS integration is contracted in this repo."""
from src.services.pms_sync import StubPmsProvider


def test_fetch_new_agreements_returns_none():
	assert StubPmsProvider().fetch_new_agreements("some_client") is None


def test_is_agreement_active_returns_none():
	assert StubPmsProvider().is_agreement_active("some_ref") is None
