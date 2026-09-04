"""SSRF/DNS-rebinding hardening tests for the website signal fetch
(Subtask 3.2.3) — this provider became reachable from untrusted public
input (self_serve_audit_worker.py) for the first time in this subtask,
so a submitted domain resolving to internal infrastructure must never
be fetched.
"""
import socket

import pytest

from src.services.owner_visibility.signals.website import (
	UnsafeFetchTargetError,
	_get_validated,
	_is_safe_host,
)


def _fake_getaddrinfo(ips):
	def _inner(hostname, port, *a, **kw):
		return [(socket.AF_INET, None, None, "", (ip, 0)) for ip in ips]
	return _inner


def test_is_safe_host_rejects_private_ip(monkeypatch):
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["10.0.0.5"]))
	assert _is_safe_host("internal.example.com") is False


def test_is_safe_host_rejects_loopback(monkeypatch):
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["127.0.0.1"]))
	assert _is_safe_host("localhost.example.com") is False


def test_is_safe_host_rejects_link_local_metadata_address(monkeypatch):
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["169.254.169.254"]))
	assert _is_safe_host("metadata.example.com") is False


def test_is_safe_host_accepts_public_ip(monkeypatch):
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34"]))
	assert _is_safe_host("example.com") is True


def test_is_safe_host_rejects_any_private_address_in_multi_result(monkeypatch):
	"""One safe and one unsafe resolved address — must reject, not pick
	the safe one (DNS-rebinding relies on exactly this ambiguity)."""
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34", "10.0.0.5"]))
	assert _is_safe_host("mixed.example.com") is False


def test_is_safe_host_rejects_unresolvable_host(monkeypatch):
	def _raise(*a, **kw):
		raise socket.gaierror("not found")
	monkeypatch.setattr(socket, "getaddrinfo", _raise)
	assert _is_safe_host("nonexistent.invalid") is False


def test_get_validated_raises_for_unsafe_initial_host(monkeypatch):
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["10.0.0.5"]))
	with pytest.raises(UnsafeFetchTargetError):
		_get_validated("https://internal.example.com/", {})


def test_get_validated_revalidates_redirect_target(monkeypatch):
	"""A redirect to a disallowed host must be rejected even though the
	initial host was safe — the whole point of per-hop re-validation."""
	import requests

	calls = {"n": 0}

	def _fake_getaddrinfo_seq(hostname, port, *a, **kw):
		if hostname == "safe.example.com":
			return [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))]
		return [(socket.AF_INET, None, None, "", ("169.254.169.254", 0))]

	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_seq)

	class _FakeResponse:
		is_redirect = True
		headers = {"location": "https://internal.example.com/steal"}

	class _FakeSession:
		def get(self, url, **kwargs):
			calls["n"] += 1
			return _FakeResponse()

	monkeypatch.setattr(requests, "Session", lambda: _FakeSession())

	with pytest.raises(UnsafeFetchTargetError):
		_get_validated("https://safe.example.com/", {})
	assert calls["n"] == 1  # rejected before following the malicious redirect
