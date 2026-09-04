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
	_FetchAborted,
	_get_validated,
	_is_safe_host,
	_read_capped,
	_safe_ip_for_host,
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


class _FakeResponse:
	def __init__(self, *, is_redirect=False, location=None, chunks=(b"<html></html>",), status_code=200, url="https://safe.example.com/"):
		self.is_redirect = is_redirect
		self.headers = {"location": location} if location else {}
		self._chunks = chunks
		self.status_code = status_code
		self.url = url
		self.closed = False

	def iter_content(self, chunk_size=8192):
		yield from self._chunks

	def close(self):
		self.closed = True


class _FakeSession:
	"""Records every URL the pinned adapter would dial. get() ignores mounts
	(the real adapter's IP-pinning is exercised separately, live)."""
	def __init__(self, responder):
		self._responder = responder
		self.gets = []

	def mount(self, prefix, adapter):
		pass

	def get(self, url, **kwargs):
		self.gets.append((url, kwargs))
		return self._responder(url, kwargs)

	def close(self):
		pass


def test_get_validated_revalidates_redirect_target(monkeypatch):
	"""A redirect to a disallowed host must be rejected even though the
	initial host was safe — the whole point of per-hop re-validation."""
	import requests

	def _fake_getaddrinfo_seq(hostname, port, *a, **kw):
		if hostname == "safe.example.com":
			return [(socket.AF_INET, None, None, "", ("93.184.216.34", 0))]
		return [(socket.AF_INET, None, None, "", ("169.254.169.254", 0))]

	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo_seq)

	session = _FakeSession(lambda url, kw: _FakeResponse(is_redirect=True, location="https://internal.example.com/steal"))
	monkeypatch.setattr(requests, "Session", lambda: session)

	with pytest.raises(UnsafeFetchTargetError):
		_get_validated("https://safe.example.com/", {})
	assert len(session.gets) == 1  # rejected before following the malicious redirect


def test_safe_ip_for_host_returns_the_validated_address(monkeypatch):
	"""The connection-pinning fix depends on this returning the exact IP that
	was validated — not just a bool — so requests can dial it directly."""
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34"]))
	assert _safe_ip_for_host("example.com") == "93.184.216.34"
	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["10.0.0.5"]))
	assert _safe_ip_for_host("internal.example.com") is None


def test_get_validated_stream_size_cap(monkeypatch):
	"""An oversized body is aborted while streaming, never fully buffered."""
	import requests
	from src.services.owner_visibility.signals import website as w

	monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo(["93.184.216.34"]))
	monkeypatch.setattr(w, "_MAX_RESPONSE_BYTES", 1024)
	big = [b"x" * 512] * 8  # 4 KiB total, over the 1 KiB cap
	session = _FakeSession(lambda url, kw: _FakeResponse(chunks=big))
	monkeypatch.setattr(requests, "Session", lambda: session)

	with pytest.raises(_FetchAborted):
		_get_validated("https://safe.example.com/", {})


def test_read_capped_enforces_absolute_deadline():
	"""A slow-drip response that never trips the read timeout is still cut
	off by the absolute wall-clock deadline."""
	class _Drip:
		def iter_content(self, chunk_size=8192):
			while True:
				yield b"x"
	with pytest.raises(_FetchAborted):
		_read_capped(_Drip(), deadline=-1.0)  # already past the deadline
