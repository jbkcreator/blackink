"""Public tracking-pixel / click-redirect endpoints (Dev Item S-8, W1
§3.1.4 "tracking pixel active" / §3.1.7 digest open/click rates). No auth —
the signed token itself is the auth, same posture as
src/api/unsubscribe_router.py, which this endpoint's shape is ported from.
No rate limiting — matching that same sibling public endpoint (a pixel/
redirect hit has no spam/cost incentive to abuse, unlike
public_landing_router.py's lead-gen form).

Endpoints:
  GET /api/v1/public/pixel?token=<signed>  — embedded in the outbound HTML
      body; returns a 1x1 GIF regardless of token validity (an invalid/
      expired token must not error visibly to the recipient's mail client
      — a broken pixel is itself a signal to any reputation-scanning
      gateway, so it degrades silently instead).
  GET /api/v1/public/click?token=<signed>  — any wrapped link in the body;
      302s to the URL embedded in the token (never a request-supplied
      target — see email_tracking.py's module docstring for why that
      closes the open-redirect risk) or a small generic error page if the
      token is invalid/expired.

Both events are deduped per dispatch_id before writing (see
src.services.events.already_logged_for_dispatch, shared with
src/services/inbound_ingest.py's email_replied producer) — the digest
computes open_rate_pct as COUNT(email_opened)/COUNT(dispatched), which only
means "percent of sent emails opened" if opens are deduped per send;
undeduped, Apple Mail Privacy Protection alone (which prefetches every
pixel once at delivery regardless of whether a human ever opens the email)
would inflate this far past 100% on repeated fetches, on top of its own
separate, unrelated-to-this-code industry-wide inflation of pixel-based
open rates in general. The application-level check is backed by a real
database-level partial unique index
(migrations/apply_events_dispatch_dedup_index.py) closing the narrow
concurrent-hit race the check alone can't (code-review fix).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.core.database import get_db_context
from src.services.email_tracking import verify_click_token, verify_pixel_token
from src.services.events import already_logged_for_dispatch, log_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/public", tags=["public-email-tracking"])

# 1x1 transparent GIF, served regardless of token validity. Standard minimal
# GIF89a byte sequence (header + logical screen descriptor + 2-color global
# color table + graphic control extension (transparent) + image descriptor
# + minimal LZW image data + trailer).
_PIXEL_GIF = bytes([
    0x47, 0x49, 0x46, 0x38, 0x39, 0x61,  # GIF89a
    0x01, 0x00, 0x01, 0x00, 0x80, 0x00, 0x00,  # logical screen descriptor
    0x00, 0x00, 0x00, 0xFF, 0xFF, 0xFF,  # global color table (black, white)
    0x21, 0xF9, 0x04, 0x01, 0x00, 0x00, 0x00, 0x00,  # graphic control extension
    0x2C, 0x00, 0x00, 0x00, 0x00, 0x01, 0x00, 0x01, 0x00, 0x00,  # image descriptor
    0x02, 0x02, 0x44, 0x01, 0x00,  # image data
    0x3B,  # trailer
])

_INVALID_CLICK_HTML = "<html><body><p>This link is invalid or has expired.</p></body></html>"


@router.get("/pixel")
def pixel(token: str = Query(...)) -> Response:
    claims = verify_pixel_token(token)
    if claims is not None:
        try:
            with get_db_context(client_id=claims.client_id) as session:
                if not already_logged_for_dispatch(session, claims.client_id, "email_opened", claims.dispatch_id):
                    log_event(
                        claims.client_id,
                        "email_opened",
                        entity_type="contact",
                        entity_id=str(claims.contact_id),
                        payload={"dispatch_id": claims.dispatch_id},
                        actor="email_tracking_pixel",
                        session=session,
                    )
                session.commit()
        except Exception:
            # Never let a DB hiccup surface as a broken pixel image — the
            # event write is best-effort observability, not the point of
            # this endpoint (the point is: always return a valid GIF).
            logger.warning("[email_tracking] pixel event write failed", exc_info=True)
    return Response(content=_PIXEL_GIF, media_type="image/gif")


@router.get("/click")
def click(token: str = Query(...)):
    claims = verify_click_token(token)
    if claims is None:
        return HTMLResponse(_INVALID_CLICK_HTML, status_code=400)

    try:
        with get_db_context(client_id=claims.client_id) as session:
            if not already_logged_for_dispatch(session, claims.client_id, "email_clicked", claims.dispatch_id):
                log_event(
                    claims.client_id,
                    "email_clicked",
                    entity_type="contact",
                    entity_id=str(claims.contact_id),
                    payload={"dispatch_id": claims.dispatch_id, "target_url": claims.target_url},
                    actor="email_tracking_click",
                    session=session,
                )
            session.commit()
    except Exception:
        # Same posture as the pixel route: a logging failure must never
        # block the recipient from actually reaching the link.
        logger.warning("[email_tracking] click event write failed", exc_info=True)

    return RedirectResponse(url=claims.target_url, status_code=302)
