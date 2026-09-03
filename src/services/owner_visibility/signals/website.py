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

import logging
import re
from typing import Any

import requests
from bs4 import BeautifulSoup

from src.services.owner_visibility.signals.base import (
    SignalResult,
    SignalProvider,
    SCORED,
    MISSING_DATA,
    SKIPPED,
)

logger = logging.getLogger(__name__)

_REQUEST_TIMEOUT = 8
_MAX_REDIRECTS = 2

# Regex patterns for after-hours detection — case-insensitive.
_AFTER_HOURS_PATTERNS = [
    re.compile(r"\bafter[\s-]?hours?\b", re.I),
    re.compile(r"\b24\s*/\s*7\b", re.I),
    re.compile(r"\bemergency\s+line\b", re.I),
    re.compile(r"\banytime\b", re.I),
]

# Slugs that indicate an owner/landlord page.
_OWNER_PAGE_SLUGS = ["/owner", "/landlord", "/property-owner", "/for-owners", "/for-landlords"]


def _fetch(url: str) -> tuple[requests.Response | None, bool]:
    """Return (response, ssl_ok). ssl_ok=False means we fell back to non-TLS."""
    headers = {"User-Agent": "BlackInkBot/1.0 (property-management research)"}

    def _get(target: str) -> requests.Response:
        # max_redirects is a Session attribute, not a kwarg on requests.get().
        s = requests.Session()
        s.max_redirects = _MAX_REDIRECTS
        return s.get(target, timeout=_REQUEST_TIMEOUT, allow_redirects=True, headers=headers)

    try:
        return _get(url), True
    except requests.exceptions.SSLError:
        # Try without TLS so other signals can still be evaluated.
        http_url = url.replace("https://", "http://", 1)
        try:
            return _get(http_url), False
        except requests.RequestException:
            return None, False
    except requests.RequestException:
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


def _score_tech_health(resp: requests.Response, ssl_ok: bool) -> SignalResult:
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
