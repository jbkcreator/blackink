import pytest
from unittest.mock import MagicMock, patch

from src.services.events import log_event, MalformedEventError, REQUIRED_PAYLOAD_FIELDS


def test_required_fields_registry_has_outbound_touch_and_ghost_shopper():
	assert REQUIRED_PAYLOAD_FIELDS["outbound_touch_dispatched"] == frozenset(
		{"touch_step", "channel", "recipient_email", "template_version", "sending_domain", "mailbox_id"}
	)
	assert REQUIRED_PAYLOAD_FIELDS["ghost_shopper_audit"] == frozenset(
		{"ghost_shopper_submitted_at", "target_domain"}
	)


def test_log_event_raises_on_missing_required_field():
	with pytest.raises(MalformedEventError):
		log_event(
			"acme_pm",
			"outbound_touch_dispatched",
			entity_type="contact",
			entity_id="123",
			payload={"touch_step": 1, "channel": "email"},  # missing 3 required keys
		)


def test_log_event_writes_row_for_unregistered_event_type():
	"""event_type with no registry entry has no required fields to enforce
	— logging must still succeed (registry is an allowlist of *extra*
	strictness, not a denylist of unknown event types)."""
	fake_session = MagicMock()
	fake_session.__enter__.return_value = fake_session
	fake_session.__exit__.return_value = False
	with patch("src.services.events.get_db_context") as mock_ctx:
		mock_ctx.return_value = fake_session
		log_event("acme_pm", "company_promoted", entity_type="company", entity_id="c1", payload={})
	fake_session.execute.assert_called_once()


def test_log_event_writes_all_required_fields_present():
	fake_session = MagicMock()
	fake_session.__enter__.return_value = fake_session
	fake_session.__exit__.return_value = False
	with patch("src.services.events.get_db_context") as mock_ctx:
		mock_ctx.return_value = fake_session
		log_event(
			"acme_pm",
			"outbound_touch_dispatched",
			entity_type="contact",
			entity_id="123",
			actor="system:sequencer",
			payload={
				"touch_step": 1,
				"channel": "email",
				"recipient_email": "j@x.com",
				"template_version": "v1",
				"sending_domain": "growth-blackink.com",
				"mailbox_id": "mbx_04",
			},
		)
	args, kwargs = fake_session.execute.call_args
	bound_params = args[1]
	assert bound_params["client_id"] == "acme_pm"
	assert bound_params["entity_type"] == "contact"
	assert bound_params["actor"] == "system:sequencer"
