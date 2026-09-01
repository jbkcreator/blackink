"""Build correctly (and incorrectly, for negative tests) signed Slack
request bodies + headers, for exercising src.services.slack.auth and
src.api.slack_router without a live Slack app.

Forked from FA/tests/test_slack_interact_endpoint.py:14-30 (_make_signed_request),
generalized to cover both Slack request shapes used in this codebase:
  - interactive payloads (block_actions / view_submission / view_closed) —
    a JSON-in-form-field body: payload=<url-encoded JSON>.
  - slash commands (/blackink-halt, /blackink-resume) — plain form-encoded,
    NOT wrapped in a "payload" field (FA/admin_router.py:2261-2262 notes
    this is a different Slack request shape from interactive callbacks).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Mapping
from urllib.parse import urlencode

DEFAULT_TEST_SECRET = "test-signing-secret"


def _sign(body: bytes, secret: str, ts: str) -> str:
	sig_base = f"v0:{ts}:{body.decode('utf-8')}"
	return "v0=" + hmac.new(secret.encode(), sig_base.encode(), hashlib.sha256).hexdigest()


def _headers(body: bytes, secret: str, ts_offset: int, bad_signature: bool) -> tuple[bytes, dict]:
	ts = str(int(time.time()) + ts_offset)
	sig = "bad-signature" if bad_signature else _sign(body, secret, ts)
	headers = {
		"content-type": "application/x-www-form-urlencoded",
		"x-slack-request-timestamp": ts,
		"x-slack-signature": sig,
	}
	return body, headers


def sign_interactive_payload(
	payload: dict,
	*,
	secret: str = DEFAULT_TEST_SECRET,
	ts_offset: int = 0,
	bad_signature: bool = False,
) -> tuple[bytes, Mapping[str, str]]:
	"""For block_actions / view_submission / view_closed payloads —
	Slack posts these as payload=<json> inside a form-urlencoded body."""
	body = urlencode({"payload": json.dumps(payload)}).encode()
	return _headers(body, secret, ts_offset, bad_signature)


def sign_slash_command(
	form: dict,
	*,
	secret: str = DEFAULT_TEST_SECRET,
	ts_offset: int = 0,
	bad_signature: bool = False,
) -> tuple[bytes, Mapping[str, str]]:
	"""For slash commands — plain form-encoded, no `payload` wrapper.
	Slack always includes `user_id` as a top-level field here (unlike
	interactive payloads, where the user is nested under payload.user.id)."""
	body = urlencode(form).encode()
	return _headers(body, secret, ts_offset, bad_signature)


def block_actions_payload(user_id: str, action_id: str, value: dict) -> dict:
	"""Shape of a Slack block_actions interaction payload, minimal fields."""
	return {
		"type": "block_actions",
		"user": {"id": user_id},
		"actions": [{"action_id": action_id, "value": json.dumps(value)}],
	}


def view_submission_payload(user_id: str, callback_id: str, private_metadata: dict, values: dict) -> dict:
	"""Shape of a Slack view_submission payload — no `actions` array."""
	return {
		"type": "view_submission",
		"user": {"id": user_id},
		"view": {
			"callback_id": callback_id,
			"private_metadata": json.dumps(private_metadata),
			"state": {"values": values},
		},
	}


def view_closed_payload(user_id: str, callback_id: str) -> dict:
	return {
		"type": "view_closed",
		"user": {"id": user_id},
		"view": {"callback_id": callback_id},
	}
