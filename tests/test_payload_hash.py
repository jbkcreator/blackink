"""Unit tests for src.services.slack.payload_hash — no live DB.

Uses a lightweight duck-typed stand-in for WorkOrder (work_orders.py is
built after this module per the plan's build sequence; payload_hash only
ever reads attributes off `order`, never constructs or imports the real
type at runtime — see the TYPE_CHECKING-only import in payload_hash.py).
"""

from dataclasses import dataclass, field
from typing import Optional

import pytest

from src.services.slack import payload_hash


@dataclass
class FakeOrder:
	client_id: str = "acme_pm"
	action_id: str = "11111111-1111-1111-1111-111111111111"
	action_class: str = "DISPATCH_EMAIL_TOUCH"
	entity_type: str = "contact"
	entity_id: str = "contact-42"
	recipient: Optional[str] = "owner@suncoastpm.com"
	payload: dict = field(default_factory=lambda: {"subject": "Hi", "body": "Hello there"})
	config_fingerprint: dict = field(
		default_factory=lambda: {
			"template_version": "v1",
			"channel": "email",
			"sending_domain": "growth-blackink.com",
			"mailbox_id": "mbx_04",
			"autonomy_band": "BAND_2_ONE_TAP",
			"offer_row_key": None,
		}
	)
	payload_hash: str = ""  # stored column; irrelevant to compute(), read by verify()


def _order(**overrides) -> FakeOrder:
	return FakeOrder(**overrides)


# ── Determinism ──────────────────────────────────────────────────────────


def test_determinism_matches_hardcoded_digest():
	"""Same order -> same digest, always. Hardcoded so an accidental
	serialization change (key order, separators, normalization) fails
	loudly here instead of silently re-baselining against itself."""
	digest = payload_hash.compute(_order())
	assert digest == "aed9ebeb9141c348f16090848adc122db893083ba3b880a7c4403c954307a64b"
	# Recomputing must be pure and stable across calls/processes.
	assert payload_hash.compute(_order()) == digest


# ── Key order irrelevance ────────────────────────────────────────────────


def test_key_order_does_not_affect_digest():
	a = _order(payload={"subject": "Hi", "body": "Hello there"})
	b = _order(payload={"body": "Hello there", "subject": "Hi"})
	assert payload_hash.compute(a) == payload_hash.compute(b)


# ── Sensitivity: one test per preimage field ────────────────────────────


def test_sensitive_to_client_id():
	assert payload_hash.compute(_order()) != payload_hash.compute(_order(client_id="other_client"))


def test_sensitive_to_action_id():
	other = "22222222-2222-2222-2222-222222222222"
	assert payload_hash.compute(_order()) != payload_hash.compute(_order(action_id=other))


def test_sensitive_to_action_class():
	assert payload_hash.compute(_order()) != payload_hash.compute(_order(action_class="ENQUEUE_DIAL_TASK"))


def test_sensitive_to_entity_type():
	assert payload_hash.compute(_order()) != payload_hash.compute(_order(entity_type="company"))


def test_sensitive_to_entity_id():
	assert payload_hash.compute(_order()) != payload_hash.compute(_order(entity_id="contact-43"))


def test_sensitive_to_recipient():
	assert payload_hash.compute(_order()) != payload_hash.compute(_order(recipient="someone-else@x.com"))


def test_sensitive_to_payload_body():
	base = _order()
	changed = _order(payload={**base.payload, "body": "Different body entirely"})
	assert payload_hash.compute(base) != payload_hash.compute(changed)


def test_sensitive_to_payload_subject():
	base = _order()
	changed = _order(payload={**base.payload, "subject": "A different subject"})
	assert payload_hash.compute(base) != payload_hash.compute(changed)


@pytest.mark.parametrize(
	"key,new_value",
	[
		("template_version", "v2"),
		("channel", "sms"),
		("sending_domain", "other-domain.com"),
		("mailbox_id", "mbx_09"),
		("autonomy_band", "BAND_3_AUTO"),
		("offer_row_key", "appt_owner_1_4"),
	],
)
def test_sensitive_to_each_config_fingerprint_key(key, new_value):
	base = _order()
	changed_config = {**base.config_fingerprint, key: new_value}
	changed = _order(config_fingerprint=changed_config)
	assert payload_hash.compute(base) != payload_hash.compute(changed)


# ── Normalization (must NOT change the digest) ──────────────────────────


def test_crlf_normalizes_same_as_lf():
	a = _order(payload={"subject": "Hi", "body": "line1\r\nline2"})
	b = _order(payload={"subject": "Hi", "body": "line1\nline2"})
	assert payload_hash.compute(a) == payload_hash.compute(b)


def test_trailing_whitespace_per_line_is_stripped():
	a = _order(payload={"subject": "Hi", "body": "line1 \nline2"})
	b = _order(payload={"subject": "Hi", "body": "line1\nline2"})
	assert payload_hash.compute(a) == payload_hash.compute(b)


def test_none_valued_key_hashes_same_as_absent_key():
	a = _order(config_fingerprint={**_order().config_fingerprint, "offer_row_key": None})
	b_config = {k: v for k, v in _order().config_fingerprint.items() if k != "offer_row_key"}
	b = _order(config_fingerprint=b_config)
	assert payload_hash.compute(a) == payload_hash.compute(b)


# ── Over-normalization guards (negative tests — these MUST differ) ─────


def test_case_is_not_folded():
	a = _order(payload={"subject": "Hi", "body": "Hello"})
	b = _order(payload={"subject": "Hi", "body": "hello"})
	assert payload_hash.compute(a) != payload_hash.compute(b)


def test_internal_whitespace_is_not_collapsed():
	a = _order(payload={"subject": "Hi", "body": "a  b"})
	b = _order(payload={"subject": "Hi", "body": "a b"})
	assert payload_hash.compute(a) != payload_hash.compute(b)


# ── verify() ─────────────────────────────────────────────────────────────


def test_verify_fresh_and_integrity_ok_when_everything_matches():
	order = _order()
	order.payload_hash = payload_hash.compute(order)
	verdict = payload_hash.verify(order, provided_hash=order.payload_hash)
	assert verdict.fresh is True
	assert verdict.integrity_ok is True


def test_verify_not_fresh_when_payload_changed_after_posting():
	order = _order()
	stale_hash = payload_hash.compute(order)  # hash carried on the posted button
	order.payload_hash = stale_hash
	order.payload = {**order.payload, "body": "Rewritten in the background"}
	verdict = payload_hash.verify(order, provided_hash=stale_hash)
	assert verdict.fresh is False
	# The stored column still matches the (now-current) content because
	# nothing updated payload without recomputing payload_hash in this
	# scenario is impossible by construction here — this test only
	# exercises the freshness check, not the integrity check.


def test_verify_integrity_not_ok_when_stored_column_is_stale():
	"""Simulates a writer that mutated `payload` without going through
	update_payload() — the bug class §5.4 check 2 exists to catch."""
	order = _order()
	order.payload_hash = payload_hash.compute(order)  # correct for the OLD payload
	order.payload = {**order.payload, "body": "Mutated without recomputing the hash"}
	current_digest = payload_hash.compute(order)
	verdict = payload_hash.verify(order, provided_hash=current_digest)
	assert verdict.fresh is True  # button carried the freshly-recomputed digest
	assert verdict.integrity_ok is False  # but the stored column disagrees
	assert verdict.stored != verdict.recomputed


def test_verify_evaluates_both_checks_even_when_both_fail():
	"""A rogue mutation trips freshness AND integrity at once — verify()
	must report both, not short-circuit on the first."""
	order = _order()
	order.payload_hash = payload_hash.compute(order)
	stale_button_hash = order.payload_hash
	order.payload = {**order.payload, "body": "changed"}
	order.payload_hash = "0" * 64  # simulate a writer that wrote garbage
	verdict = payload_hash.verify(order, provided_hash=stale_button_hash)
	assert verdict.fresh is False
	assert verdict.integrity_ok is False


@pytest.mark.parametrize("bad_value", [None, 12345, ["not", "a", "string"], {"nope": True}, 3.14, True])
def test_verify_fails_closed_on_non_string_provided_hash(bad_value):
	"""hmac.compare_digest raises TypeError on a type mismatch (confirmed
	empirically: hmac.compare_digest("x", None) raises) — provided_hash
	comes from Slack-relayed, JSON-parsed input at the router, so a
	malformed/missing/wrong-type value must fail closed, never crash the
	request. This is the crash this test guards against."""
	order = _order()
	order.payload_hash = payload_hash.compute(order)
	verdict = payload_hash.verify(order, provided_hash=bad_value)
	assert verdict.fresh is False
	assert verdict.integrity_ok is True  # stored column is still internally correct


def test_verify_empty_string_provided_hash_is_not_fresh():
	order = _order()
	order.payload_hash = payload_hash.compute(order)
	verdict = payload_hash.verify(order, provided_hash="")
	assert verdict.fresh is False


# ── hash_version stability ──────────────────────────────────────────────


def test_hash_version_is_stamped_and_stable():
	assert payload_hash.HASH_VERSION == 1
	preimage = payload_hash._preimage(_order())
	assert preimage["v"] == 1


# ── button_value ─────────────────────────────────────────────────────────


def test_button_value_recomputes_never_reads_stored_column():
	order = _order()
	order.payload_hash = "stale-garbage-not-a-real-hash"
	import json

	value = json.loads(payload_hash.button_value(order, decision="APPROVED"))
	assert value["payload_hash"] == payload_hash.compute(order)
	assert value["payload_hash"] != order.payload_hash
	assert value["client_id"] == order.client_id
	assert value["action_id"] == str(order.action_id)
	assert value["decision"] == "APPROVED"
