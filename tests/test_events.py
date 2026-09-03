"""Tests for src/services/events.py."""

from unittest.mock import MagicMock

from src.services.events import log_touch_dispatched


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
    )
    sql = str(session.execute.call_args_list[0][0][0])
    assert "outbound_touch_dispatched" in sql


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
    )
    params = session.execute.call_args_list[0][0][1]
    assert params["client_id"] == "client_b"


def test_log_touch_dispatched_payload_contains_sending_domain():
    import json
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
    )
    params = session.execute.call_args_list[0][0][1]
    payload = json.loads(params["payload"])
    assert payload["sending_domain"] == "blackink-out.io"
    assert payload["touch_step"] == 1
