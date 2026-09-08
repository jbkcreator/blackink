"""Public one-click email unsubscribe endpoint — CLAUDE.md's mandatory
"Outbound email — mandatory one-click unsubscribe" invariant (confirmed
2026-09-07). No auth — the signed token itself is the auth, same posture as
ForcedAction-System/Forced-action-'s src/api/email_unsubscribe_router.py,
which this endpoint's shape is ported from.

Endpoints:
  GET  /api/v1/public/unsubscribe?token=<signed>  — human clicking the link
  POST /api/v1/public/unsubscribe?token=<signed>  — RFC 8058 one-click
       unsubscribe (mailbox providers POST here because our
       List-Unsubscribe-Post header advertises List-Unsubscribe=One-Click;
       without this route those requests 405 and the provider treats the
       header as a lie)
Both run the same suppression logic and are idempotent.

The token embeds client_id (src/services/email_unsubscribe.py) so a click
only ever suppresses within the client it was minted for — no cross-client
suppression side-channel.
"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import HTMLResponse
from sqlalchemy import text

from src.core.database import get_db_context
from src.services.email_unsubscribe import verify_unsubscribe_token
from src.services.winback_sequencer import stop_active_winback_runs

router = APIRouter(prefix="/api/v1/public", tags=["public-unsubscribe"])

_CONFIRMED_HTML = "<html><body><p>You have been unsubscribed and will not receive further emails from us.</p></body></html>"
_INVALID_HTML = "<html><body><p>This unsubscribe link is invalid or has expired.</p></body></html>"


def _do_unsubscribe(token: str) -> HTMLResponse:
	parsed = verify_unsubscribe_token(token)
	if not parsed:
		return HTMLResponse(_INVALID_HTML, status_code=400)
	client_id, email = parsed

	with get_db_context(client_id=client_id) as session:
		# Safe no-op if the email doesn't match a row in a given table for
		# this client — a token only ever touches the client_id it was
		# minted for.
		session.execute(
			text("UPDATE contacts SET is_opted_out = TRUE WHERE lower(email) = lower(:email)"),
			{"email": email},
		)
		stop_active_winback_runs(session, client_id, email, "OPT_OUT")
		session.commit()

	return HTMLResponse(_CONFIRMED_HTML, status_code=200)


@router.get("/unsubscribe")
def unsubscribe(token: str = Query(...)) -> HTMLResponse:
	return _do_unsubscribe(token)


@router.post("/unsubscribe")
def unsubscribe_one_click(token: str = Query(...)) -> HTMLResponse:
	"""RFC 8058 one-click unsubscribe — mailbox providers POST here, no body
	parsing needed (the token is in the query string on both verbs)."""
	return _do_unsubscribe(token)
