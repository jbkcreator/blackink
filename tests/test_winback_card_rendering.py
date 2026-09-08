"""Tests for the DISPATCH_WINBACK_TOUCH Slack approval card (Subtask 3.1.2,
audit finding #6) — a dedicated rich card, not the generic text fallback
the cold sequence's DISPATCH_EMAIL_TOUCH already had before this."""

from types import SimpleNamespace

from src.services.slack.listeners import _card_text, _card_text_blocks


def _order(**payload_overrides):
	payload = {
		"winback_row_id": 7,
		"touch_step": 2,
		"subject": "Re: An update on 1 Main St",
		"body": "Hi there,\n\nFollowing up on my note.\n\nBest,\nThe Blackink team",
		"template_version": "wb2_v1",
	}
	payload.update(payload_overrides)
	return SimpleNamespace(
		client_id="client_a",
		action_id="action-abcdef12",
		action_class="DISPATCH_WINBACK_TOUCH",
		entity_type="winback_row",
		entity_id=str(payload["winback_row_id"]),
		recipient="owner@example.com",
		payload=payload,
		config_fingerprint={"channel": "winback", "winback_row_id": payload["winback_row_id"], "touch_step": payload["touch_step"]},
	)


def test_card_text_shows_subject_and_body_for_winback_touch():
	text = _card_text(_order())
	assert "Win-Back Touch 2" in text
	assert "owner@example.com" in text
	assert "Re: An update on 1 Main St" in text
	assert "Following up on my note" in text


def test_card_text_flags_missing_content_rather_than_hiding_it():
	text = _card_text(_order(body=None, subject=None))
	assert "approved copy missing" in text.lower()


def test_card_blocks_use_dedicated_winback_layout_not_generic_fallback():
	blocks = _card_text_blocks(_order())
	header = blocks[0]
	assert header["type"] == "header"
	assert "Win-Back Touch 2" in header["text"]["text"]

	fields_block = blocks[1]
	field_texts = " ".join(f["text"] for f in fields_block["fields"])
	assert "owner@example.com" in field_texts
	assert "Step 2 of 3" in field_texts  # not "of 5" — the cold sequence's step count


def test_card_blocks_show_winback_row_id_not_run_id():
	blocks = _card_text_blocks(_order(winback_row_id=42))
	context_block = next(b for b in blocks if b["type"] == "context")
	assert "winback_row `42`" in context_block["elements"][0]["text"]


def test_card_blocks_include_approve_reject_buttons():
	blocks = _card_text_blocks(_order())
	action_block = next(b for b in blocks if b["type"] == "actions")
	button_action_ids = {el["action_id"] for el in action_block["elements"]}
	assert {"approve", "reject"} <= button_action_ids
