import json

import pytest
from unittest.mock import MagicMock, patch

from src.services.events import log_event, log_touch_dispatched, MalformedEventError, REQUIRED_PAYLOAD_FIELDS


def test_log_touch_dispatched_inserts_correct_event_type():
    session = MagicMock()
    log_touch_dispatched(
        session=session,
        client_id="client_a",
        contact_id=1,
        touch_step=1,
        dispatch_id="d-uuid",
        mailbox_id=7,
        sending_domain="out.io",
        template_version="v1",
        recipient_email="a@out.io",
    )
    params = session.execute.call_args_list[0][0][1]
    assert params["event_type"] == "outbound_touch_dispatched"


def test_log_touch_dispatched_includes_client_id_in_params():
    session = MagicMock()
    log_touch_dispatched(
        session=session,
        client_id="client_b",
        contact_id=2,
        touch_step=3,
        dispatch_id="d-uuid",
        mailbox_id=5,
        sending_domain="out.io",
        template_version="v1",
        recipient_email="b@out.io",
    )
    params = session.execute.call_args_list[0][0][1]
    assert params["client_id"] == "client_b"


def test_log_touch_dispatched_payload_contains_sending_domain():
    session = MagicMock()
    log_touch_dispatched(
        session=session,
        client_id="client_a",
        contact_id=1,
        touch_step=1,
        dispatch_id="d-uuid",
        mailbox_id=7,
        sending_domain="blackink-out.io",
        template_version="v1",
        recipient_email="a@out.io",
    )
    params = session.execute.call_args_list[0][0][1]
    payload = json.loads(params["payload"])
    assert payload["sending_domain"] == "blackink-out.io"
    assert payload["touch_step"] == 1
    assert payload["channel"] == "email"
    assert payload["recipient_email"] == "a@out.io"


def test_required_fields_registry_has_outbound_touch_and_owner_score():
	assert REQUIRED_PAYLOAD_FIELDS["outbound_touch_dispatched"] == frozenset(
		{"touch_step", "channel", "recipient_email", "template_version", "sending_domain", "mailbox_id"}
	)
	assert REQUIRED_PAYLOAD_FIELDS["owner_score_generated"] == frozenset(
		{"score_total", "county", "data_coverage_pct", "county_rank"}
	)


def test_ghost_shopper_audit_is_not_in_the_registry():
	"""Ghost-Shopper is permanently deferred, replaced by the Owner
	Visibility Score engine (v2 blueprint §3.1.3) — a live registry entry
	for an event type nothing will ever emit again is dead config."""
	assert "ghost_shopper_audit" not in REQUIRED_PAYLOAD_FIELDS


def test_log_event_raises_on_missing_owner_score_field():
	with pytest.raises(MalformedEventError):
		log_event(
			"acme_pm",
			"owner_score_generated",
			entity_type="company",
			entity_id="c1",
			payload={"score_total": 82, "county": "hillsborough_fl", "data_coverage_pct": 91},  # missing county_rank
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


def test_log_event_with_session_does_not_open_new_db_context():
	"""When a caller passes its own session (e.g. promotion_sweep joining its
	own already-open system-role transaction), log_event must not call
	get_db_context — that would open a second connection under a different
	role and commit independently of the caller's transaction."""
	fake_session = MagicMock()
	with patch("src.services.events.get_db_context") as mock_ctx:
		log_event(
			"acme_pm",
			"company_promoted",
			entity_type="company",
			entity_id="c1",
			payload={},
			session=fake_session,
		)
		mock_ctx.assert_not_called()
	fake_session.execute.assert_called_once()


def test_log_event_without_session_still_opens_db_context():
	"""session=None (the default) must keep the pre-existing behavior."""
	fake_session = MagicMock()
	fake_session.__enter__.return_value = fake_session
	fake_session.__exit__.return_value = False
	with patch("src.services.events.get_db_context") as mock_ctx:
		mock_ctx.return_value = fake_session
		log_event("acme_pm", "company_promoted", entity_type="company", entity_id="c1", payload={})
		mock_ctx.assert_called_once()
	fake_session.execute.assert_called_once()
