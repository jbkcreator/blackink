"""Resolves the single booking-page redirect target for the no-show
recovery email and the self-serve landing page (Subtask 3.2.3).

Neither 3.2.1 nor 3.2.2 built any self-serve "pick an open time slot"
surface — both only receive webhook events for meetings already booked
elsewhere. GoHighLevel is different: a GHL calendar has a real public
booking page. This module never fabricates a redirect: it selects the
single calendar_connections row an operator has explicitly flagged
is_default_sales_booking=TRUE (never "first row by connection_id" — an
arbitrary, silent choice), and only returns a URL that is https and on
the configured host allowlist.

GHL prefill uses urlencode (never raw string concatenation). The exact
GHL query-param names are a manual verification item against a real
GHL calendar's public booking-page contract before this ships — not
assumed here; PREFILL_PARAMS below is the one place to correct once
verified. Google/Microsoft links are used exactly as stored, no
prefill attempted — that gap is explicitly left open (same posture as
3.2.2's reschedule-link gap).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlencode, urlparse

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings

# Manual verification item — not confirmed against a real GHL calendar's
# public booking-page contract. Correct here once verified; until then
# GHL prefill is best-effort, never claimed complete in the DoD sense.
_GHL_PREFILL_PARAMS = {"name": "first_name", "email": "email"}


@dataclass(frozen=True)
class BookingLink:
	url: str
	prefilled: bool


def _is_valid_public_url(public_booking_url: Optional[str]) -> bool:
	"""Shared https-only + host-allowlist check — never a fabricated or
	unsafe redirect, for any connection_scope."""
	if not public_booking_url:
		return False
	parsed = urlparse(public_booking_url)
	if parsed.scheme != "https":
		return False
	allowed_hosts = get_settings().booking_redirect_allowed_hosts
	return bool(allowed_hosts) and (parsed.hostname or "").lower() in allowed_hosts


def _apply_ghl_prefill(public_booking_url: str, *, name: Optional[str], email: Optional[str]) -> BookingLink:
	params = {}
	if name:
		params[_GHL_PREFILL_PARAMS["name"]] = name
	if email:
		params[_GHL_PREFILL_PARAMS["email"]] = email
	if not params:
		return BookingLink(url=public_booking_url, prefilled=False)
	parsed = urlparse(public_booking_url)
	separator = "&" if parsed.query else "?"
	return BookingLink(url=f"{public_booking_url}{separator}{urlencode(params)}", prefilled=True)


def resolve_booking_link(session: Session, *, name: Optional[str] = None, email: Optional[str] = None) -> Optional[BookingLink]:
	"""Returns None (never a broken/fabricated redirect) if no connection
	is flagged default, if it has no public_booking_url, if the URL isn't
	https, or if its host isn't in settings.booking_redirect_allowed_hosts.
	Callers must handle None with a 'we'll follow up by email' fallback."""
	row = session.execute(
		text(
			"SELECT provider, public_booking_url FROM calendar_connections "
			"WHERE connection_scope = 'INTERNAL_SALES_DEMO' AND is_default_sales_booking = TRUE "
			"AND status = 'ACTIVE' LIMIT 1"
		)
	).first()
	if row is None or not _is_valid_public_url(row.public_booking_url):
		return None

	if row.provider == "GOHIGHLEVEL":
		return _apply_ghl_prefill(row.public_booking_url, name=name, email=email)

	return BookingLink(url=row.public_booking_url, prefilled=False)


def resolve_owner_booking_link(
	session: Session, client_id: str, *, name: Optional[str] = None, email: Optional[str] = None
) -> Optional[BookingLink]:
	"""CLIENT_OWNER_BOOKING counterpart to resolve_booking_link() (Subtask
	3.1.2) — a property owner booking the *client's* own calendar, not
	Blackink's sales calendar. Per-client default (unlike
	is_default_sales_booking's single global default — each PM-firm client
	has its own calendar), so client_id is required, not optional.

	name/email are optional GHL prefill params, same as resolve_booking_link
	— pass the specific owner's own name/email (not a batch-level default)
	so a GHL booking page prefills correctly per recipient.

	Returns None (never a fabricated link) until an operator has flagged a
	CLIENT_OWNER_BOOKING connection for this client with
	is_default_owner_booking=TRUE and a real https public_booking_url —
	callers (winback_content.render_winback_touch's Touch 3) must handle
	None with a 'just reply' fallback, same posture as resolve_booking_link."""
	row = session.execute(
		text(
			"SELECT provider, public_booking_url FROM calendar_connections "
			"WHERE client_id = :client_id AND connection_scope = 'CLIENT_OWNER_BOOKING' "
			"AND is_default_owner_booking = TRUE AND status = 'ACTIVE' LIMIT 1"
		),
		{"client_id": client_id},
	).first()
	if row is None or not _is_valid_public_url(row.public_booking_url):
		return None

	if row.provider == "GOHIGHLEVEL":
		return _apply_ghl_prefill(row.public_booking_url, name=name, email=email)

	return BookingLink(url=row.public_booking_url, prefilled=False)
