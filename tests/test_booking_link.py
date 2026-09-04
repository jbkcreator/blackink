"""Unit tests for src/services/booking_link.py's resolve_booking_link()
(Subtask 3.2.3) — no live DB required, using a FakeSession stand-in for
the one query it issues (same pattern as tests/test_compliance_gate.py's
FakeSession for _check_non_poach).
"""
from types import SimpleNamespace

import pytest

from config.settings import get_settings
from src.services.booking_link import resolve_booking_link


class FakeSession:
	def __init__(self, row=None):
		self._row = row

	def execute(self, *args, **kwargs):
		return SimpleNamespace(first=lambda: self._row)


@pytest.fixture(autouse=True)
def _allowed_hosts(monkeypatch):
	monkeypatch.setattr(
		get_settings().__class__, "booking_redirect_allowed_hosts",
		property(lambda self: ("book.example-ghl.com", "calendar.google.com")),
	)


def test_returns_none_when_no_default_connection():
	assert resolve_booking_link(FakeSession(row=None)) is None


def test_returns_none_when_public_booking_url_missing():
	row = SimpleNamespace(provider="GOOGLE", public_booking_url=None)
	assert resolve_booking_link(FakeSession(row=row)) is None


def test_returns_none_when_not_https():
	row = SimpleNamespace(provider="GOOGLE", public_booking_url="http://calendar.google.com/abc")
	assert resolve_booking_link(FakeSession(row=row)) is None


def test_returns_none_when_host_not_allowlisted():
	row = SimpleNamespace(provider="GOOGLE", public_booking_url="https://evil.example.com/abc")
	assert resolve_booking_link(FakeSession(row=row)) is None


def test_google_link_returned_as_is_no_prefill():
	row = SimpleNamespace(provider="GOOGLE", public_booking_url="https://calendar.google.com/appt/abc")
	link = resolve_booking_link(FakeSession(row=row), name="Sam", email="sam@example.com")
	assert link.url == "https://calendar.google.com/appt/abc"
	assert link.prefilled is False


def test_ghl_link_prefilled_and_url_encoded():
	row = SimpleNamespace(provider="GOHIGHLEVEL", public_booking_url="https://book.example-ghl.com/widget")
	link = resolve_booking_link(FakeSession(row=row), name="Sam Owner", email="sam+test@example.com")
	assert link.prefilled is True
	assert link.url.startswith("https://book.example-ghl.com/widget?")
	assert "sam%2Btest%40example.com" in link.url or "sam%40example.com" not in link.url  # urlencoded, not raw
	assert " " not in link.url


def test_ghl_link_with_no_name_or_email_is_unprefilled():
	row = SimpleNamespace(provider="GOHIGHLEVEL", public_booking_url="https://book.example-ghl.com/widget")
	link = resolve_booking_link(FakeSession(row=row))
	assert link.url == "https://book.example-ghl.com/widget"
	assert link.prefilled is False


def test_returns_none_when_no_hosts_configured(monkeypatch):
	monkeypatch.setattr(get_settings().__class__, "booking_redirect_allowed_hosts", property(lambda self: ()))
	row = SimpleNamespace(provider="GOOGLE", public_booking_url="https://calendar.google.com/appt/abc")
	assert resolve_booking_link(FakeSession(row=row)) is None
