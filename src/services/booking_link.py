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
	if row is None or not row.public_booking_url:
		return None

	parsed = urlparse(row.public_booking_url)
	if parsed.scheme != "https":
		return None
	allowed_hosts = get_settings().booking_redirect_allowed_hosts
	if not allowed_hosts or (parsed.hostname or "").lower() not in allowed_hosts:
		return None

	if row.provider == "GOHIGHLEVEL":
		params = {}
		if name:
			params[_GHL_PREFILL_PARAMS["name"]] = name
		if email:
			params[_GHL_PREFILL_PARAMS["email"]] = email
		if params:
			separator = "&" if parsed.query else "?"
			return BookingLink(url=f"{row.public_booking_url}{separator}{urlencode(params)}", prefilled=True)

	return BookingLink(url=row.public_booking_url, prefilled=False)
