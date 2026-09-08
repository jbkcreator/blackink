"""Config-driven pay-per-lead portal parsers (Task 4.2.3).

Path B (Mailgun inbound email — see src/api/mailgun_inbound_router.py) receives
lead-notification emails forwarded from third-party listing/lead portals. This
module recognises the portal that sent a given email and extracts structured
fields (name / email / phone / property address / inquiry text) from its
notification-email body, tagging the refined ``source_channel``.

Design — config-driven registry (the hard DoD line)
====================================================
A portal matcher is **global reference data**, identical for every tenant — it
describes an outside vendor's email format, not anything owned by a client — so
it lives here as an in-code registry, NOT a tenant-bearing table (no RLS row in
config/tenant_policies.py, no migration). Adding a 4th portal requires exactly:

    1. one new ``PortalConfig`` row appended to ``PORTAL_REGISTRY``, and
    2. one new parser function it points at.

Nothing in the router (``mailgun_inbound``) or the orchestrator
(``run_inbound_pipeline``) changes. The dispatcher ``classify_and_parse`` picks
the first config whose matcher matches the sender/subject and calls its parser.

Fallback (the other hard DoD line)
==================================
An email matching NO portal config falls through to the ``UNCLASSIFIED`` bucket
with ``requires_human_review=True`` — the row is still written and still surfaces
for a human; nothing is dropped and nothing crashes.

Resilience
==========
Every parser degrades gracefully: a parser whose portal matched but which fails
to extract a field returns whatever it got plus ``requires_human_review=True``,
and any exception inside a parser is caught by the dispatcher and converted into
an ``UNCLASSIFIED`` review row — a parser MUST never take down the webhook.

NOTE ON SAMPLES: the regexes below are written against *representative* portal
notification-email layouts (documented public field sets), NOT emails captured
from production accounts (those samples were a HITL task, ticket 11, still open).
They are intentionally lenient and self-heal to human review on any miss, so a
real-format drift degrades to a reviewable row rather than a wrong extraction.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

UNCLASSIFIED = "UNCLASSIFIED"


@dataclass
class ParsedLead:
    """Result of classifying + parsing one inbound portal email."""

    source_channel: str
    prospect_name: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    property_address: Optional[str] = None
    inquiry_text: Optional[str] = None
    # True when the row must be surfaced for a human: either no portal matched
    # (UNCLASSIFIED) or a matched portal parser could not extract the core
    # fields. Threaded through to inbound_messages.requires_human_review.
    requires_human_review: bool = False


# A parser takes the raw email parts and returns a ParsedLead. It must NOT raise
# for ordinary "field missing" cases — it returns partial data with
# requires_human_review=True. The dispatcher guards against unexpected raises.
ParserFn = Callable[["EmailParts"], ParsedLead]


@dataclass
class EmailParts:
    sender: str          # raw From, e.g. 'APM Leads <leads@allpropertymanagement.com>'
    subject: str
    body_plain: str
    body_html: Optional[str] = None
    # Email already teased out of the raw From header by the router, if any —
    # used as a fallback contact when the body carries no explicit email.
    fallback_email: Optional[str] = None


@dataclass
class PortalConfig:
    """One registry row. ``sender_pattern`` and/or ``subject_pattern`` are
    case-insensitive regexes; a config matches when every provided pattern
    matches (a config with neither never matches). First match wins."""

    name: str
    source_channel: str
    parser: ParserFn
    sender_pattern: Optional[str] = None
    subject_pattern: Optional[str] = None
    _sender_re: Optional[re.Pattern] = field(default=None, init=False, repr=False)
    _subject_re: Optional[re.Pattern] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.sender_pattern:
            self._sender_re = re.compile(self.sender_pattern, re.IGNORECASE)
        if self.subject_pattern:
            self._subject_re = re.compile(self.subject_pattern, re.IGNORECASE)

    def matches(self, parts: EmailParts) -> bool:
        if self._sender_re is None and self._subject_re is None:
            return False
        if self._sender_re is not None and not self._sender_re.search(parts.sender or ""):
            return False
        if self._subject_re is not None and not self._subject_re.search(parts.subject or ""):
            return False
        return True


# ── Shared extraction helpers ────────────────────────────────────────────────

_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
# North-American style phone; lenient, digits normalised afterwards.
_PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}")
_MONEY_RE = re.compile(r"\$\s?\d[\d,]*(?:\.\d{2})?(?:\s?[-–to]{1,3}\s?\$?\d[\d,]*)?")


def _labeled(body: str, *labels: str) -> Optional[str]:
    """Return the value following the first matching ``Label:`` line.

    Matches ``Label: value`` on one line (value = rest of line) case-insensitively.
    Labels may be regex-alternatives; the first label that hits wins."""
    for label in labels:
        m = re.search(
            rf"^[ \t>*]*{label}[ \t]*:[ \t]*(.+?)[ \t]*$",
            body,
            re.IGNORECASE | re.MULTILINE,
        )
        if m:
            val = m.group(1).strip()
            if val:
                return val
    return None


def _first_email(*candidates: Optional[str]) -> Optional[str]:
    for c in candidates:
        if not c:
            continue
        m = _EMAIL_RE.search(c)
        if m:
            return m.group(0).lower()
    return None


def _first_phone(*candidates: Optional[str]) -> Optional[str]:
    for c in candidates:
        if not c:
            continue
        m = _PHONE_RE.search(c)
        if m:
            return m.group(0).strip()
    return None


# ── Per-portal parsers ───────────────────────────────────────────────────────
#
# Each parser follows the same shape: pull labelled fields, fall back to
# free-text scans, and flag requires_human_review when the two identifying
# fields (a name AND at least one contact handle) could not both be found.

def _finalize(
    source_channel: str,
    *,
    name: Optional[str],
    email: Optional[str],
    phone: Optional[str],
    address: Optional[str],
    inquiry: Optional[str],
) -> ParsedLead:
    # Core signal for a usable lead: a name plus at least one way to reach them.
    usable = bool(name) and bool(email or phone)
    return ParsedLead(
        source_channel=source_channel,
        prospect_name=name,
        email=email,
        phone=phone,
        property_address=address,
        inquiry_text=(inquiry or None),
        requires_human_review=not usable,
    )


def parse_apm(parts: EmailParts) -> ParsedLead:
    """All Property Management — labelled 'Name/Email/Phone/Property/Message' block."""
    body = parts.body_plain or ""
    name = _labeled(body, "Owner Name", "Name", "Contact", "Lead Name")
    email = _first_email(_labeled(body, "Email", "Email Address"))
    phone = _first_phone(_labeled(body, "Phone", "Phone Number", "Telephone"))
    address = _labeled(body, "Property Address", "Property", "Address", "Location")
    inquiry = _labeled(body, "Message", "Details", "Comments", "Notes") or body
    # Body-wide fallbacks if the labels drifted.
    email = email or _first_email(body)
    phone = phone or _first_phone(body)
    return _finalize("APM", name=name, email=email, phone=phone, address=address, inquiry=inquiry)


def parse_manage_my_property(parts: EmailParts) -> ParsedLead:
    """Manage My Property — similar labelled layout, different label wording."""
    body = parts.body_plain or ""
    name = _labeled(body, "Owner", "Owner Name", "Full Name", "Name", "From")
    email = _first_email(_labeled(body, "Email", "E-mail", "Email Address")) or _first_email(body)
    phone = _first_phone(_labeled(body, "Phone", "Phone Number", "Best Contact Number")) or _first_phone(body)
    address = _labeled(body, "Property Address", "Property Location", "Property", "Address")
    inquiry = _labeled(body, "Message", "Inquiry", "Comments", "Additional Details", "Notes") or body
    return _finalize(
        "MANAGE_MY_PROPERTY", name=name, email=email, phone=phone, address=address, inquiry=inquiry
    )


def parse_thumbtack(parts: EmailParts) -> ParsedLead:
    """Thumbtack — name, job description, optional budget, contact details.

    Thumbtack notification emails lead with the requester name and a job/service
    description; budget (when present) is a dollar amount. We fold job
    description + budget into inquiry_text so downstream (Respond queue, closer
    card) sees the full context, since inbound_messages has no dedicated budget
    column."""
    body = parts.body_plain or ""
    name = _labeled(body, "Name", "Customer", "Requested by", "From")
    email = _first_email(_labeled(body, "Email")) or _first_email(body)
    phone = _first_phone(_labeled(body, "Phone", "Phone Number")) or _first_phone(body)
    address = _labeled(body, "Location", "Address", "Service Address", "Property Address")
    job = _labeled(body, "Job", "Service", "Project", "Job Description", "Details", "Service Needed")

    budget = _labeled(body, "Budget", "Estimated Budget", "Price")
    if not budget:
        m = _MONEY_RE.search(body)
        budget = m.group(0).strip() if m else None

    parts_txt = []
    if job:
        parts_txt.append(f"Job: {job}")
    if budget:
        parts_txt.append(f"Budget: {budget}")
    # Always keep the raw body too so nothing is lost.
    inquiry = "\n".join(parts_txt) if parts_txt else None
    if inquiry:
        inquiry = f"{inquiry}\n\n{body}".strip()
    else:
        inquiry = body

    return _finalize("THUMBTACK", name=name, email=email, phone=phone, address=address, inquiry=inquiry)


# ── The registry (the config-driven surface) ─────────────────────────────────
#
# Adding a 4th portal = append one PortalConfig here + write its parser fn above.
# No router/orchestrator change is required. First matching config wins.

PORTAL_REGISTRY: List[PortalConfig] = [
    PortalConfig(
        name="All Property Management",
        source_channel="APM",
        parser=parse_apm,
        sender_pattern=r"allpropertymanagement\.com|@apm\.|All Property Management",
        subject_pattern=None,
    ),
    PortalConfig(
        name="Manage My Property",
        source_channel="MANAGE_MY_PROPERTY",
        parser=parse_manage_my_property,
        sender_pattern=r"managemyproperty\.com|Manage\s?My\s?Property",
        subject_pattern=None,
    ),
    PortalConfig(
        name="Thumbtack",
        source_channel="THUMBTACK",
        parser=parse_thumbtack,
        sender_pattern=r"thumbtack\.com|Thumbtack",
        subject_pattern=None,
    ),
]


def classify_and_parse(parts: EmailParts) -> ParsedLead:
    """Dispatch: find the first registry config that matches and run its parser.

    Guarantees (both hard DoD lines):
      * No match  -> ParsedLead(source_channel='UNCLASSIFIED', requires_human_review=True),
        carrying the router-derived fallback email and the raw body, so the row
        is still written and still surfaces for a human.
      * Parser raises unexpectedly -> same UNCLASSIFIED review fallback (a
        buggy/format-drifted parser can never crash the webhook or drop a lead).
    """
    for config in PORTAL_REGISTRY:
        try:
            if config.matches(parts):
                result = config.parser(parts)
                # Defensive: if a parser handed back an email of None but the
                # router already derived one from the From header, keep it.
                if result.email is None and parts.fallback_email:
                    result.email = parts.fallback_email
                return result
        except Exception:  # noqa: BLE001 — resilience is the whole point here
            logger.exception(
                "[portal_parsers] parser %s raised — falling through to UNCLASSIFIED",
                config.name,
            )
            break

    return ParsedLead(
        source_channel=UNCLASSIFIED,
        prospect_name=None,
        email=parts.fallback_email,
        phone=None,
        property_address=None,
        inquiry_text=(parts.body_plain or None),
        requires_human_review=True,
    )
