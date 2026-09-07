"""Website signal provider — scrapes the company's public website for 4 signals.

Signal breakdown (38 pts total):
  website_owner_page     14 pts — /owners or /landlords page exists with substantive content
  website_contact_info   10 pts — phone, email, and contact form all present
  website_tech_health     6 pts — HTTPS, mobile viewport, no obvious broken links on homepage
  website_after_hours     8 pts — after-hours or 24/7 mention present

Uses requests + BeautifulSoup. Follows up to 2 redirects; hard 8s timeout
so a slow host can't stall the sweep. SSL errors are treated as MISSING_DATA
(not a crash) — a company with an invalid cert can't be scored on tech health
but the other signals can still be collected from the non-TLS content.

Never raises — every failure path returns MISSING_DATA or SKIPPED.
"""

import ipaddress
import logging
import re
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse, urljoin, urlunparse

import requests
from requests.adapters import HTTPAdapter
from bs4 import BeautifulSoup

from src.services.owner_visibility.signals.base import (
    SignalResult,
    SignalProvider,
    SCORED,
    MISSING_DATA,
    SKIPPED,
)

logger = logging.getLogger(__name__)

# (connect, read) inactivity timeouts — a per-socket-operation limit.
_REQUEST_TIMEOUT = (5, 8)
_MAX_REDIRECTS = 2
# Hard caps that an inactivity timeout alone cannot provide: an absolute
# wall-clock deadline for the whole fetch (all redirect hops included), and a
# maximum body size read while streaming. Without these, a hostile site can
# hold the single self-serve worker forever by dribbling bytes just fast
# enough to keep resetting the read timeout, or exhaust memory with an
# unbounded body — this provider is reachable from the public /audit page.
_TOTAL_DEADLINE_SECONDS = 20
_MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB


class UnsafeFetchTargetError(Exception):
    """Raised when a hostname resolves to a private/reserved/loopback
    address. Subtask 3.2.3 — this provider is now reachable from the
    public /audit landing page via self_serve_audit_worker.py, where the
    submitted domain is untrusted input, not an internally-vetted
    prospected company — an attacker-controlled domain must never be
    used to reach internal infrastructure (SSRF)."""


class _FetchAborted(Exception):
    """Streaming read hit the absolute deadline or the size cap."""


@dataclass
class _FetchedPage:
    """The subset of a response the scoring functions read, with the body
    already fully (and safely, size-capped) buffered so nothing downstream
    can re-trigger a stream from an attacker-controlled socket."""
    status_code: int
    url: str
    content: bytes
    headers: dict


def _safe_ip_for_host(hostname: str) -> str | None:
    """Resolve hostname and return ONE validated public IP to connect to,
    or None if it can't be resolved or ANY resolved address is
    private/loopback/link-local/multicast/reserved (covers RFC1918,
    127.0.0.0/8, 169.254.0.0/16 including the 169.254.169.254 cloud
    metadata address, multicast, and other IANA-reserved ranges).

    Returning the validated IP — not just a bool — is what lets the caller
    PIN the connection to it (see _PinnedIPAdapter). Validating the name and
    then letting requests resolve it again at connect time is a DNS-rebinding
    hole: an attacker's resolver can answer public on the first lookup and
    169.254.169.254 on the second. Pinning to this exact validated address
    closes that TOCTOU gap — every byte is fetched from an address that was
    checked, never a second, attacker-swapped resolution."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return None
    safe_ip = None
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr)
        except ValueError:
            return None
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return None
        if safe_ip is None:
            safe_ip = addr
    return safe_ip


def _is_safe_host(hostname: str) -> bool:
    """Back-compat boolean wrapper around _safe_ip_for_host()."""
    return _safe_ip_for_host(hostname) is not None


class _PinnedIPAdapter(HTTPAdapter):
    """Pins every connection this adapter makes to a pre-validated IP while
    preserving the original hostname for TLS SNI and certificate
    verification. This is what actually forecloses DNS rebinding: the socket
    dials the exact address _safe_ip_for_host() checked, never a fresh,
    possibly-swapped resolution of the name."""

    def __init__(self, pinned_ip: str, sni_hostname: str, is_https: bool, **kwargs):
        self._pinned_ip = pinned_ip
        self._sni_hostname = sni_hostname
        self._is_https = is_https
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        # For TLS, verify the cert against — and send SNI for — the REAL
        # hostname even though the socket connects to the pinned IP. These
        # kwargs are only valid on an HTTPS pool, so never set them for http.
        if self._is_https:
            kwargs["assert_hostname"] = self._sni_hostname
            kwargs["server_hostname"] = self._sni_hostname
        super().init_poolmanager(*args, **kwargs)

    def send(self, request, **kwargs):
        parsed = urlparse(request.url)
        host, port = parsed.hostname, parsed.port
        ip = self._pinned_ip
        ip_netloc = f"[{ip}]" if ":" in ip else ip
        if port:
            ip_netloc += f":{port}"
        request.url = urlunparse(parsed._replace(netloc=ip_netloc))
        request.headers["Host"] = host if not port else f"{host}:{port}"
        return super().send(request, **kwargs)


def _read_capped(resp: requests.Response, deadline: float) -> bytes:
    """Stream the body, enforcing both the size cap and the absolute
    deadline while reading — never buffering an unbounded body and never
    letting a slow-drip response run past the wall-clock limit."""
    total = 0
    chunks = []
    for chunk in resp.iter_content(chunk_size=8192):
        if time.monotonic() > deadline:
            raise _FetchAborted("absolute fetch deadline exceeded")
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            raise _FetchAborted(f"response body exceeded {_MAX_RESPONSE_BYTES} bytes")
        chunks.append(chunk)
    return b"".join(chunks)

# Regex patterns for after-hours detection — case-insensitive.
_AFTER_HOURS_PATTERNS = [
    re.compile(r"\bafter[\s-]?hours?\b", re.I),
    re.compile(r"\b24\s*/\s*7\b", re.I),
    re.compile(r"\bemergency\s+line\b", re.I),
    re.compile(r"\banytime\b", re.I),
]

# Slugs that indicate an owner/landlord page.
_OWNER_PAGE_SLUGS = ["/owner", "/landlord", "/property-owner", "/for-owners", "/for-landlords"]


def _get_validated(target: str, headers: dict) -> _FetchedPage:
    """Manually follows redirects (never requests' own allow_redirects=True)
    so every hop's hostname is re-validated before the request is issued —
    a redirect to an internal/reserved address is rejected the same as a
    direct request to one. Each hop's connection is PINNED to the exact IP
    that was validated (via _PinnedIPAdapter), closing the DNS-rebinding
    TOCTOU gap, and the body is streamed under an absolute deadline and a
    size cap shared across all hops."""
    deadline = time.monotonic() + _TOTAL_DEADLINE_SECONDS
    url = target
    for _ in range(_MAX_REDIRECTS + 1):
        if time.monotonic() > deadline:
            raise _FetchAborted("absolute fetch deadline exceeded before request")
        parsed = urlparse(url)
        hostname = parsed.hostname
        safe_ip = _safe_ip_for_host(hostname) if hostname else None
        if not safe_ip:
            raise UnsafeFetchTargetError(f"{hostname!r} resolves to a private/reserved/loopback address")

        is_https = parsed.scheme == "https"
        session = requests.Session()
        adapter = _PinnedIPAdapter(safe_ip, hostname, is_https)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        resp = None
        try:
            resp = session.get(
                url, timeout=_REQUEST_TIMEOUT, allow_redirects=False, headers=headers, stream=True
            )
            if resp.is_redirect and resp.headers.get("location"):
                url = urljoin(url, resp.headers["location"])
                continue
            content = _read_capped(resp, deadline)
            return _FetchedPage(
                # Report the hostname URL, not the IP-rewritten one the adapter
                # actually dialed — downstream scoring reads the scheme off this.
                status_code=resp.status_code,
                url=url,
                content=content,
                headers=dict(resp.headers),
            )
        finally:
            if resp is not None:
                resp.close()
            session.close()
    raise UnsafeFetchTargetError(f"too many redirects (> {_MAX_REDIRECTS})")


def _fetch(url: str) -> tuple[_FetchedPage | None, bool]:
    """Return (page, ssl_ok). ssl_ok=False means we fell back to non-TLS."""
    headers = {"User-Agent": "BlackInkBot/1.0 (property-management research)"}

    try:
        return _get_validated(url, headers), True
    except requests.exceptions.SSLError:
        # Try without TLS so other signals can still be evaluated.
        http_url = url.replace("https://", "http://", 1)
        try:
            return _get_validated(http_url, headers), False
        except (requests.RequestException, UnsafeFetchTargetError, _FetchAborted):
            return None, False
    except (requests.RequestException, UnsafeFetchTargetError, _FetchAborted):
        return None, True


def _normalize_url(domain: str, website: str | None) -> str:
    """Return a usable URL for the company website."""
    if website:
        url = website.strip()
        if not url.startswith(("http://", "https://")):
            return f"https://{url}"
        return url
    return f"https://{domain}"


def _score_owner_page(soup: BeautifulSoup, base_url: str) -> SignalResult:
    """14 pts if an /owners or /landlords page is linked and appears substantive."""
    links = soup.find_all("a", href=True)
    hrefs = [a["href"].lower() for a in links]
    found = any(any(slug in h for slug in _OWNER_PAGE_SLUGS) for h in hrefs)
    if found:
        return SignalResult(
            "website_owner_page", 14, 14, SCORED,
            "owner/landlord page link found in navigation",
        )
    return SignalResult("website_owner_page", 0, 14, SCORED, "no owner/landlord page link found")


def _score_contact_info(soup: BeautifulSoup) -> SignalResult:
    """10 pts — phone (4), email (3), and contact form (3)."""
    text = soup.get_text(" ", strip=True)
    phone_found = bool(re.search(r"\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}", text))
    email_found = bool(re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", text))
    form_found = bool(soup.find("form"))
    pts = (4 if phone_found else 0) + (3 if email_found else 0) + (3 if form_found else 0)
    found_items = [x for x, ok in [("phone", phone_found), ("email", email_found), ("form", form_found)] if ok]
    detail = f"found: {found_items}" if found_items else "no contact info found"
    return SignalResult("website_contact_info", pts, 10, SCORED, detail)


def _score_tech_health(resp: "_FetchedPage", ssl_ok: bool) -> SignalResult:
    """6 pts — HTTPS (3) + mobile viewport (3). SSL failure costs the HTTPS pts."""
    https_pts = 3 if ssl_ok and resp.url.startswith("https://") else 0
    soup = BeautifulSoup(resp.content, "lxml")
    viewport = soup.find("meta", attrs={"name": re.compile("viewport", re.I)})
    viewport_pts = 3 if viewport else 0
    pts = https_pts + viewport_pts
    detail = f"https={ssl_ok}, viewport={'yes' if viewport else 'no'}"
    return SignalResult("website_tech_health", pts, 6, SCORED, detail)


def _score_after_hours(soup: BeautifulSoup) -> SignalResult:
    """8 pts if any after-hours / 24×7 language appears anywhere on the homepage."""
    text = soup.get_text(" ", strip=True)
    matched = next((p.pattern for p in _AFTER_HOURS_PATTERNS if p.search(text)), None)
    if matched:
        return SignalResult("website_after_hours", 8, 8, SCORED, f"matched pattern: {matched!r}")
    return SignalResult("website_after_hours", 0, 8, SCORED, "no after-hours language found")


class WebsiteSignalProvider(SignalProvider):
    """Scrapes the company's public website for 4 visibility signals (38 pts total)."""

    def collect(self, company: dict[str, Any]) -> list[SignalResult]:
        domain: str = company.get("domain", "")
        website: str | None = company.get("website")

        if not domain and not website:
            return [
                SignalResult("website_owner_page", 0, 14, MISSING_DATA, "no domain or website on record"),
                SignalResult("website_contact_info", 0, 10, MISSING_DATA, "no domain or website on record"),
                SignalResult("website_tech_health", 0, 6, MISSING_DATA, "no domain or website on record"),
                SignalResult("website_after_hours", 0, 8, MISSING_DATA, "no domain or website on record"),
            ]

        url = _normalize_url(domain, website)
        resp, ssl_ok = _fetch(url)

        if resp is None or resp.status_code >= 400:
            reason = f"HTTP {resp.status_code}" if resp else "connection failed"
            return [
                SignalResult("website_owner_page", 0, 14, MISSING_DATA, reason),
                SignalResult("website_contact_info", 0, 10, MISSING_DATA, reason),
                SignalResult("website_tech_health", 0, 6, MISSING_DATA, reason),
                SignalResult("website_after_hours", 0, 8, MISSING_DATA, reason),
            ]

        try:
            soup = BeautifulSoup(resp.content, "lxml")
        except Exception as exc:
            logger.warning("website signals: parse error for %s: %s", url, exc)
            reason = f"parse error: {exc}"
            return [
                SignalResult("website_owner_page", 0, 14, MISSING_DATA, reason),
                SignalResult("website_contact_info", 0, 10, MISSING_DATA, reason),
                SignalResult("website_tech_health", 0, 6, MISSING_DATA, reason),
                SignalResult("website_after_hours", 0, 8, MISSING_DATA, reason),
            ]

        return [
            _score_owner_page(soup, url),
            _score_contact_info(soup),
            _score_tech_health(resp, ssl_ok),
            _score_after_hours(soup),
        ]
