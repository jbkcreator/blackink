"""Unit tests for src/services/show_rate_reminders._fetch_ovs_pdf() —
contacts.ovs_pdf_url is written by Dev 2's (not yet built) storage step,
so this codebase treats it as untrusted input. No live DB needed: these
tests exercise the pure fetch/validation logic against a fake httpx
streaming response, not a real network call.
"""

from __future__ import annotations

import httpx
import pytest

from config.settings import get_settings
from src.services.show_rate_reminders import (
    UnsafeOvsPdfUrlError,
    _OVS_PDF_MAX_BYTES,
    _fetch_ovs_pdf,
)


class _FakeStreamResponse:
    def __init__(self, *, status_code=200, content_type="application/pdf", chunks=(b"%PDF-1.4\n...",)):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._chunks = chunks

    def iter_bytes(self):
        yield from self._chunks

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def approved_host(monkeypatch):
    monkeypatch.setattr(get_settings(), "ovs_pdf_allowed_hosts_raw", "approved-bucket.example.com")
    yield "approved-bucket.example.com"


def test_rejects_non_https_scheme(approved_host):
    with pytest.raises(UnsafeOvsPdfUrlError, match="https"):
        _fetch_ovs_pdf(f"http://{approved_host}/report.pdf")


def test_rejects_unconfigured_allowlist(monkeypatch):
    monkeypatch.setattr(get_settings(), "ovs_pdf_allowed_hosts_raw", "")
    with pytest.raises(UnsafeOvsPdfUrlError, match="not configured"):
        _fetch_ovs_pdf("https://anywhere.example.com/report.pdf")


def test_rejects_host_not_in_allowlist(approved_host):
    with pytest.raises(UnsafeOvsPdfUrlError, match="allowlist"):
        _fetch_ovs_pdf("https://attacker-controlled.example.com/report.pdf")


def test_rejects_non_200_status(approved_host, monkeypatch):
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStreamResponse(status_code=404))
    with pytest.raises(UnsafeOvsPdfUrlError, match="404"):
        _fetch_ovs_pdf(f"https://{approved_host}/missing.pdf")


def test_rejects_wrong_content_type(approved_host, monkeypatch):
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStreamResponse(content_type="text/html"))
    with pytest.raises(UnsafeOvsPdfUrlError, match="Content-Type"):
        _fetch_ovs_pdf(f"https://{approved_host}/report.pdf")


def test_rejects_body_without_pdf_magic_bytes(approved_host, monkeypatch):
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStreamResponse(chunks=(b"not a pdf",)))
    with pytest.raises(UnsafeOvsPdfUrlError, match="magic bytes"):
        _fetch_ovs_pdf(f"https://{approved_host}/report.pdf")


def test_rejects_body_exceeding_max_size(approved_host, monkeypatch):
    oversized_chunk = b"%PDF-1.4\n" + b"A" * (_OVS_PDF_MAX_BYTES + 1)
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStreamResponse(chunks=(oversized_chunk,)))
    with pytest.raises(UnsafeOvsPdfUrlError, match="exceeds"):
        _fetch_ovs_pdf(f"https://{approved_host}/report.pdf")


def test_enforces_size_cap_while_streaming_even_with_many_small_chunks(approved_host, monkeypatch):
    """A malicious/misconfigured host could omit or lie about
    Content-Length — the cap must be enforced against the actual bytes
    received, not a trusted header, so this feeds it in small chunks
    that only exceed the cap cumulatively."""
    chunk = b"A" * 1024
    n_chunks = (_OVS_PDF_MAX_BYTES // 1024) + 10
    chunks = (b"%PDF-1.4\n",) + tuple(chunk for _ in range(n_chunks))
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStreamResponse(chunks=chunks))
    with pytest.raises(UnsafeOvsPdfUrlError, match="exceeds"):
        _fetch_ovs_pdf(f"https://{approved_host}/report.pdf")


def test_passes_follow_redirects_false(approved_host, monkeypatch):
    captured = {}

    def fake_stream(method, url, **kwargs):
        captured.update(kwargs)
        return _FakeStreamResponse()

    monkeypatch.setattr(httpx, "stream", fake_stream)
    _fetch_ovs_pdf(f"https://{approved_host}/report.pdf")
    assert captured.get("follow_redirects") is False


def test_accepts_a_real_valid_pdf(approved_host, monkeypatch):
    monkeypatch.setattr(httpx, "stream", lambda *a, **k: _FakeStreamResponse())
    body = _fetch_ovs_pdf(f"https://{approved_host}/report.pdf")
    assert body.startswith(b"%PDF-")
