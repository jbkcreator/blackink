"""Payload-bound hash verification for Slack action cards.

Blueprint §3.0.3 / §5.1: every interactive card is bound to a SHA-256 hash
of its exact message payload, recipient identifier, and configuration
state, so a click on a card whose content changed after posting is
rejected rather than silently executed against stale-approved content.

No Forced Action equivalent exists — FA's button values carry only
{item_id, action} (FA/src/services/relay/slack_post.py:61-62), with zero
binding to message content. This module is net-new (Dev 3 plan §5).

The preimage, its serialization, and the normalization rules below are
FIXED (see Tasks/dev-3-slack-agent-hub-week0.md §5.2) — changing any of
them changes what digest an unchanged order produces, which must be
versioned (HASH_VERSION), not silently altered.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
	# Import for the type hint only — work_orders imports this module at
	# runtime (enqueue() computes payload_hash before INSERT), so a
	# top-level import here would be circular. WorkOrder is accessed
	# purely by attribute below; nothing here depends on its identity.
	from src.services.work_orders import WorkOrder

HASH_VERSION = 1


@dataclass(frozen=True)
class HashVerdict:
	"""Result of verifying a button's carried hash against an order's
	current content. Both checks are always computed — never
	short-circuited — so a rogue mutation that trips both at once (stale
	click AND stored-column drift) is never misreported as an ordinary
	stale click. See §5.4."""

	fresh: bool  # button hash matches content recomputed right now
	integrity_ok: bool  # order.payload_hash matches content recomputed right now
	recomputed: str
	provided: str
	stored: str


def _normalize_string(value: str) -> str:
	"""Line-ending and trailing-whitespace normalization only — the
	minimum needed to survive a Slack round-trip or a JSONB write/read
	without flipping the digest for content nobody actually changed.
	Deliberately NOT case folding, NOT whitespace collapsing, NOT HTML
	stripping: those would let a real content change hash identically,
	which defeats the whole control. See §5.2's over-normalization
	guards."""
	normalized = value.replace("\r\n", "\n").replace("\r", "\n")
	lines = [line.rstrip() for line in normalized.split("\n")]
	return "\n".join(lines)


def _normalize(value: Any) -> Any:
	"""Recursive normalization of a preimage field. Drops None-valued
	dict keys so an absent key and an explicit null hash identically —
	a JSONB round-trip through Postgres can flip one into the other, and
	without this a legitimate, content-unchanged card would fail
	verification."""
	if isinstance(value, dict):
		return {k: _normalize(v) for k, v in value.items() if v is not None}
	if isinstance(value, (list, tuple)):
		return [_normalize(v) for v in value]
	if isinstance(value, str):
		return _normalize_string(value)
	if isinstance(value, Decimal):
		return str(value)
	if isinstance(value, datetime):
		dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
		return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
	if isinstance(value, date):
		return value.isoformat()
	return value


def _preimage(order: "WorkOrder") -> dict:
	return {
		"v": HASH_VERSION,
		"client_id": order.client_id,
		"action_id": str(order.action_id),
		"action_class": order.action_class,
		"entity_type": order.entity_type,
		"entity_id": order.entity_id,
		"recipient": order.recipient or "",
		"payload": _normalize(order.payload),
		"config": _normalize(order.config_fingerprint),
	}


def _canonical_bytes(preimage: dict) -> bytes:
	return json.dumps(
		preimage,
		sort_keys=True,
		separators=(",", ":"),
		ensure_ascii=False,
		allow_nan=False,
	).encode("utf-8")


def compute(order: "WorkOrder") -> str:
	"""The canonical digest for an order's CURRENT content. Called at
	enqueue time (before INSERT — action_id must already be assigned in
	application code, see the models.py docstring), at update_payload
	time, and at every click/submission to recompute for comparison."""
	return hashlib.sha256(_canonical_bytes(_preimage(order))).hexdigest()


def verify(order: "WorkOrder", provided_hash: Any) -> HashVerdict:
	"""Two independent, unconditionally-evaluated comparisons — see
	HashVerdict's docstring for why neither short-circuits the other.

	provided_hash is typed Any, not str: it originates as a value parsed
	out of a Slack button's JSON `value` field (src.api.slack_router, not
	yet built), which is attacker-influenced input even though the
	request as a whole is Slack-signature-verified — a malformed, missing,
	or wrong-type payload_hash key must fail closed (not fresh), never
	raise. hmac.compare_digest itself requires both arguments be the same
	type (str/str or bytes/bytes) and raises TypeError otherwise —
	confirmed empirically (`hmac.compare_digest("x", None)` raises) — so a
	provided_hash of None, an int, or any non-str would otherwise crash
	this security-critical comparison instead of cleanly rejecting the
	click. Coerced to "" (a value that can never equal a real 64-char
	digest) rather than raising."""
	recomputed = compute(order)
	safe_provided = provided_hash if isinstance(provided_hash, str) else ""
	return HashVerdict(
		fresh=hmac.compare_digest(recomputed, safe_provided),
		integrity_ok=hmac.compare_digest(recomputed, order.payload_hash),
		recomputed=recomputed,
		provided=safe_provided,
		stored=order.payload_hash,
	)


def button_value(order: "WorkOrder", decision: str) -> str:
	"""JSON string for a Slack button's `value` field. payload_hash here
	is RECOMPUTED, never order.payload_hash read off the row — see the
	rationale in Tasks/dev-3-slack-agent-hub-week0.md §5.3: a button built
	from a stale stored column would carry a digest that fails its own
	verification on the first click, including the refreshed card posted
	by the rejection path itself."""
	return json.dumps(
		{
			"client_id": order.client_id,
			"action_id": str(order.action_id),
			"decision": decision,
			"payload_hash": compute(order),
		},
		separators=(",", ":"),
	)
