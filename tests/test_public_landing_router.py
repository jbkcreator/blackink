"""Unit tests for the pure validation logic in
src/api/public_landing_router.py (Subtask 3.2.3) — domain/SSRF
validation and the honeypot check. No live DB or FastAPI TestClient
needed for these; the full POST-handler flow (rate limiting, DB
insert, event logging, redirect resolution) needs live Postgres +
Redis and is exercised manually per the plan's Verification section.
"""
import socket
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from src.api.public_landing_router import (
	AuditSubmission,
	_RateLimitUnavailable,
	_check_rate_limit,
	_is_ip_literal,
	_validate_domain,
	_INVALID_HOST_SUFFIXES,
	_INVALID_HOSTS,
)


def _fake_getaddrinfo(ips):
	def _inner(hostname, port, *a, **kw):
		return [(socket.AF_INET, None, None, "", (ip, 0)) for ip in ips]
	return _inner


def test_is_ip_literal_true_for_ipv4():
	assert _is_ip_literal("93.184.216.34") is True


def test_is_ip_literal_true_for_ipv6():
	assert _is_ip_literal("::1") is True


def test_is_ip_literal_false_for_domain():
	assert _is_ip_literal("example.com") is False


def test_validate_domain_rejects_ip_literal():
	assert _validate_domain("93.184.216.34") is False


def test_validate_domain_rejects_localhost():
	assert "localhost" in _INVALID_HOSTS
	assert _validate_domain("localhost") is False


def test_validate_domain_rejects_dot_local():
	assert _validate_domain("myserver.local") is False


def test_validate_domain_rejects_no_dot_bare_host():
	assert _validate_domain("intranet") is False


def test_validate_domain_rejects_empty():
	assert _validate_domain("") is False


def test_validate_domain_rejects_private_resolved_address(monkeypatch):
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["10.0.0.5"]))
	assert _validate_domain("internal.example.com") is False


def test_validate_domain_accepts_public_domain(monkeypatch):
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34"]))
	assert _validate_domain("example.com") is True


# ── AuditSubmission required-field validation ───────────────────────────

_VALID_PAYLOAD = {
	"domain": "example.com", "name": "Sam Owner", "email": "sam@example.com",
	"company": "Acme PM", "county_slug": "hillsborough_fl",
}


def test_valid_payload_accepted():
	sub = AuditSubmission(**_VALID_PAYLOAD)
	assert sub.name == "Sam Owner"
	assert sub.email == "sam@example.com"
	assert sub.company == "Acme PM"


@pytest.mark.parametrize("missing_field", ["name", "email", "company", "domain", "county_slug"])
def test_missing_required_field_rejected(missing_field):
	payload = dict(_VALID_PAYLOAD)
	del payload[missing_field]
	with pytest.raises(ValidationError):
		AuditSubmission(**payload)


@pytest.mark.parametrize("missing_field", ["name", "email", "company"])
def test_blank_required_field_rejected(missing_field):
	payload = dict(_VALID_PAYLOAD)
	payload[missing_field] = ""
	with pytest.raises(ValidationError):
		AuditSubmission(**payload)


def test_invalid_email_format_rejected():
	payload = dict(_VALID_PAYLOAD)
	payload["email"] = "not-an-email"
	with pytest.raises(ValidationError):
		AuditSubmission(**payload)


def test_domain_too_short_rejected():
	payload = dict(_VALID_PAYLOAD)
	payload["domain"] = "ab"
	with pytest.raises(ValidationError):
		AuditSubmission(**payload)


def test_overlong_name_rejected():
	payload = dict(_VALID_PAYLOAD)
	payload["name"] = "x" * 201
	with pytest.raises(ValidationError):
		AuditSubmission(**payload)


# ── Rate limiting fails closed on Redis errors ──────────────────────────

def _fake_request():
	return SimpleNamespace(client=SimpleNamespace(host="203.0.113.5"))


def test_check_rate_limit_raises_when_redis_unavailable(monkeypatch):
	def _raise(*a, **kw):
		raise ConnectionError("redis down")
	monkeypatch.setattr("src.api.public_landing_router.get_redis_client", lambda: SimpleNamespace(incr=_raise))
	with pytest.raises(_RateLimitUnavailable):
		_check_rate_limit(_fake_request())


def test_check_rate_limit_true_over_threshold(monkeypatch):
	monkeypatch.setattr(
		"src.api.public_landing_router.get_settings",
		lambda: SimpleNamespace(self_serve_rate_limit_per_10min=5),
	)
	fake_redis = MagicMock()
	fake_redis.incr.return_value = 6
	monkeypatch.setattr("src.api.public_landing_router.get_redis_client", lambda: fake_redis)
	assert _check_rate_limit(_fake_request()) is True


def test_check_rate_limit_false_under_threshold(monkeypatch):
	monkeypatch.setattr(
		"src.api.public_landing_router.get_settings",
		lambda: SimpleNamespace(self_serve_rate_limit_per_10min=5),
	)
	fake_redis = MagicMock()
	fake_redis.incr.return_value = 1
	monkeypatch.setattr("src.api.public_landing_router.get_redis_client", lambda: fake_redis)
	assert _check_rate_limit(_fake_request()) is False
