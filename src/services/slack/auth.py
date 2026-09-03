"""Slack request authenticity and user authorization.

Two distinct concerns kept in one small module because both answer the
same question — "is this inbound Slack request allowed" — from two
different angles:
  verify_slack_signature: is this request genuinely FROM Slack?
  approver_authorized:    which Slack WORKSPACE MEMBER sent it, and are
                           they allowed to decide?
A valid Slack signature proves only the former. It says nothing about who
clicked — Forced Action's admin_router.py:1786-1790 documents the exact
bug this second check exists to prevent: "if approvers and user_id not in
approvers" silently authorizes EVERY workspace member whenever the
allowlist is empty/unset, because an empty list is falsy and the `if`
short-circuits to a no-op. Both checks here are FAIL CLOSED: an
empty/unset allowlist means NOBODY is authorized, not everybody.

verify_slack_signature is a direct fork of
FA/src/api/admin_router.py:1602-1621, with one deliberate change: reads
the secret via get_settings() at call time rather than a module-level
`settings` binding, since FA's own test suite documents that binding
desyncing from a freshly-reloaded config.settings.settings object
(FA/tests/test_slack_interact_endpoint.py:50-61).
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Mapping, Optional

from config.settings import get_settings

_REPLAY_WINDOW_SECONDS = 300


def verify_slack_signature(headers: Mapping[str, str], body: bytes) -> bool:
	"""HMAC-SHA256 over 'v0:{timestamp}:{raw_body}', constant-time compare.
	Rejects requests whose timestamp is more than 5 minutes old or in the
	future, per Slack's own replay-protection guidance."""
	settings = get_settings()
	secret = settings.slack_signing_secret
	if not secret:
		return False

	ts = headers.get("x-slack-request-timestamp", "")
	try:
		if abs(time.time() - int(ts)) > _REPLAY_WINDOW_SECONDS:
			return False
	except (TypeError, ValueError):
		return False

	sig_base = f"v0:{ts}:{body.decode('utf-8')}"
	expected = "v0=" + hmac.new(
		secret.get_secret_value().encode(),
		sig_base.encode(),
		hashlib.sha256,
	).hexdigest()
	received = headers.get("x-slack-signature", "")
	return hmac.compare_digest(expected, received)


def approver_authorized(user_id: str, *, client_id: Optional[str] = None) -> bool:
	"""FAIL CLOSED. The fleet-wide (global) allowlist is checked first —
	no DB read, no cache to skew, reads settings directly (FA's own
	rationale at admin_router.py:1804-1812: this ordering means the
	cheapest, most-reliable check runs before anything that could fail or
	go stale).

	`client_id` is accepted now for signature stability with the Week 1
	per-tenant approver widening (Dev 3 plan §7.4/§9 item 7 — needs a
	`clients` column that does not exist yet). It is currently unused:
	Week 0 ships global-allowlist-only, and both lists empty must still
	resolve to False, so leaving the parameter unused changes nothing
	about the fail-closed guarantee.
	"""
	if not user_id:
		return False

	settings = get_settings()
	fleet_approvers = settings.blackink_global_approvers or ()
	return user_id in fleet_approvers
