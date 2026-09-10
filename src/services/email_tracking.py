"""Tracking pixel / click-wrap tokens for outbound cold-sequence email
(Dev Item S-8, W1 §3.1.4 "tracking pixel active" / §3.1.7 digest open/click
rates). Mirrors src/services/email_unsubscribe.py's mint/verify/url shape
exactly (stateless HS256 JWT, no DB row per token, a dedicated `_TOKEN_TYPE`
per token kind so a pixel token can never be replayed as a click token or
vice versa) — see that module's own docstring for the pattern this copies.

Scope, resolved during task-analysis (see
docs/plans/2026-09-10-s8-s11-tracking-and-reply-send.md): only the cold
5-touch sequence feeds the digest's `outbound_touch_dispatched` denominator
(src/tasks/daily_digest.py), so this module is wired from
src/services/sequence_orchestrator.py only — the separate booking-
confirmation/show-rate-reminder email stack (src/services/email_dispatch.py)
is out of scope, it doesn't feed that denominator.

Click tokens carry their OWN destination URL, chosen by US at send time —
the public click endpoint never takes an attacker-supplied redirect target
from the request itself, only from inside a token we signed. This closes
the obvious open-redirect risk without needing a DB lookup at click time.

dispatch_id is embedded in both token kinds so the resulting `email_opened`/
`email_clicked` event can be deduped per send (see email_tracking_router.py)
and so Evidence Packet §2 can join back to the specific dispatch.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import NamedTuple, Optional

import jwt

from config.settings import get_settings

_ALGORITHM = "HS256"
_PIXEL_TOKEN_TYPE = "email_pixel"
_CLICK_TOKEN_TYPE = "email_click"
# Long expiry, matching email_unsubscribe.py's own reasoning: a recipient
# may open a months-old email and the pixel/link must still resolve.
_DEFAULT_EXPIRY = timedelta(days=365)


def _secret() -> str:
    settings = get_settings()
    secret = settings.email_tracking_secret
    if not secret:
        raise RuntimeError(
            "Tracking tokens need EMAIL_TRACKING_SECRET configured "
            "(config/settings.py's email_tracking_secret)"
        )
    return secret.get_secret_value()


class PixelClaims(NamedTuple):
    client_id: str
    contact_id: int
    dispatch_id: str


class ClickClaims(NamedTuple):
    client_id: str
    contact_id: int
    dispatch_id: str
    target_url: str


def mint_pixel_token(client_id: str, contact_id: int, dispatch_id: str, expires_in: timedelta = _DEFAULT_EXPIRY) -> str:
    payload = {
        "client_id": client_id,
        "contact_id": contact_id,
        "dispatch_id": dispatch_id,
        "type": _PIXEL_TOKEN_TYPE,
        "exp": datetime.now(timezone.utc) + expires_in,
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def verify_pixel_token(token: str) -> Optional[PixelClaims]:
    """Returns the claims, or None if invalid, expired, or wrong token type.
    Never raises — a broken pixel is itself a (harmless) signal, not an
    error worth surfacing to the recipient's mail client."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != _PIXEL_TOKEN_TYPE:
        return None
    client_id = payload.get("client_id")
    contact_id = payload.get("contact_id")
    dispatch_id = payload.get("dispatch_id")
    if not client_id or contact_id is None or not dispatch_id:
        return None
    return PixelClaims(client_id=client_id, contact_id=contact_id, dispatch_id=dispatch_id)


def pixel_url(client_id: str, contact_id: int, dispatch_id: str) -> str:
    """Embeddable <img> src, landing on the public pixel endpoint
    (src/api/email_tracking_router.py)."""
    base = get_settings().app_base_url.rstrip("/")
    token = mint_pixel_token(client_id, contact_id, dispatch_id)
    return f"{base}/api/v1/public/pixel?token={token}"


def mint_click_token(
    client_id: str, contact_id: int, dispatch_id: str, target_url: str, expires_in: timedelta = _DEFAULT_EXPIRY
) -> str:
    payload = {
        "client_id": client_id,
        "contact_id": contact_id,
        "dispatch_id": dispatch_id,
        "target_url": target_url,
        "type": _CLICK_TOKEN_TYPE,
        "exp": datetime.now(timezone.utc) + expires_in,
    }
    return jwt.encode(payload, _secret(), algorithm=_ALGORITHM)


def verify_click_token(token: str) -> Optional[ClickClaims]:
    """Returns the claims (including the ONLY place the redirect target
    comes from), or None if invalid, expired, or wrong token type."""
    try:
        payload = jwt.decode(token, _secret(), algorithms=[_ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("type") != _CLICK_TOKEN_TYPE:
        return None
    client_id = payload.get("client_id")
    contact_id = payload.get("contact_id")
    dispatch_id = payload.get("dispatch_id")
    target_url = payload.get("target_url")
    if not client_id or contact_id is None or not dispatch_id or not target_url:
        return None
    return ClickClaims(client_id=client_id, contact_id=contact_id, dispatch_id=dispatch_id, target_url=target_url)


def html_body_with_pixel(plain_text_body: str, pixel_img_url: str) -> str:
    """Minimal HTML rendering of a plain-text email body plus a trailing,
    invisible tracking pixel. Deliberately NOT a redesign — this repo's
    touch copy (src/services/sequence_content.py) is plain text with no
    branding/HTML today (that's dev item S-5's job, not this one); this
    exists purely so a pixel can be embedded, preserving line breaks and
    nothing else. HTML-escapes the body first (a template render of a
    contact's own name/company could otherwise inject markup)."""
    import html as _html

    escaped = _html.escape(plain_text_body)
    html_lines = escaped.replace("\n", "<br>\n")
    pixel_tag = f'<img src="{_html.escape(pixel_img_url)}" width="1" height="1" alt="" style="display:none">'
    return f"<html><body>{html_lines}{pixel_tag}</body></html>"


def wrap_link(client_id: str, contact_id: int, dispatch_id: str, target_url: str) -> str:
    """Rewrites a real URL into a click-tracked redirect link for embedding
    in an outbound email body. The unsubscribe link is deliberately NEVER
    passed through this — RFC 8058/Gmail-Yahoo bulk-sender rules govern that
    link's own behavior, and routing it through an extra redirect hop adds
    risk (if this endpoint ever misbehaves) for no benefit; see
    docs/plans/2026-09-10-s8-s11-tracking-and-reply-send.md's S-8 section."""
    base = get_settings().app_base_url.rstrip("/")
    token = mint_click_token(client_id, contact_id, dispatch_id, target_url)
    return f"{base}/api/v1/public/click?token={token}"
