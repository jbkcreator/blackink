"""Follow-up finding #2: enrolled email touches must carry approved content,
so an approved touch actually sends (not NO_CONTENT -> FAILED).

Covers: render_touch produces real copy; enroll_contact persists
subject/body/template_version into email-touch payloads (and nothing for the
manual phone/LinkedIn touches); and the persisted content flows through the
dispatcher to the sender end to end.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.services import sequence_enrollment
from src.services.sequence_content import render_touch, EMAIL_TOUCH_STEPS


@pytest.fixture(autouse=True)
def _stub_unsubscribe():
    """Subtask 3.1.2's mandatory one-click unsubscribe footer/header is
    computed unconditionally on every send path that reaches sender.send()
    — stub it here since this suite never configures EMAIL_UNSUBSCRIBE_SECRET."""
    with (
        patch("src.services.sequence_orchestrator.unsubscribe_url", return_value="https://app.example.com/unsub?token=t"),
        patch("src.services.sequence_orchestrator.append_unsubscribe_footer", side_effect=lambda body, url: body),
        # S-8's tracking pixel — same reasoning as the unsubscribe stub
        # above: pixel_url() needs EMAIL_TRACKING_SECRET configured, which
        # this suite deliberately never sets.
        patch("src.services.sequence_orchestrator.pixel_url", return_value="https://app.example.com/pixel?token=p"),
        # Code-review fix (PR #50): dispatch_touch() now validates the
        # tracking secret before claim_touch() — stub it the same way.
        patch("src.services.sequence_orchestrator.assert_tracking_configured"),
    ):
        yield


def test_render_touch_has_real_non_placeholder_copy():
    for step in EMAIL_TOUCH_STEPS:
        subject, body, tv = render_touch(step, first_name="Dana", company_name="Acme PM")
        assert subject and body
        assert "placeholder" not in body.lower()
        assert "generated upstream" not in body.lower()
        assert tv == f"t{step}_v1"
    # touches 3 and 5 thread as replies
    assert render_touch(3)[0].startswith("Re:")
    assert render_touch(5)[0].startswith("Re:")


def test_render_touch_rejects_non_email_step():
    with pytest.raises(ValueError):
        render_touch(2)


def _enqueue_capture(monkeypatch):
    captured = []

    def _fake_enqueue(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(sequence_enrollment, "may_enroll", lambda cid: True)
    from src.services import work_orders as wo
    monkeypatch.setattr(wo, "enqueue", _fake_enqueue)
    return captured


def test_enroll_persists_content_for_email_touches_only(monkeypatch):
    captured = _enqueue_capture(monkeypatch)
    session = MagicMock()

    run_id = sequence_enrollment.enroll_contact(
        session, "client_a", 42, "owner@acme.com",
        first_name="Dana", company_name="Acme PM",
    )
    assert run_id is not None
    # Touch 2 (dial) is NOT enrolled — it is posted event-driven on Touch 1
    # approval (ADR 0001), so enrollment enqueues only steps 1, 3, 4, 5.
    assert len(captured) == 4

    by_step = {c["payload"]["touch_step"]: c for c in captured}
    assert 2 not in by_step
    # Email touches carry approved copy.
    for step in (1, 3, 5):
        p = by_step[step]["payload"]
        assert p["subject"] and p["body"]
        assert p["template_version"] == f"t{step}_v1"
        assert by_step[step]["config_fingerprint"]["channel"] == "setter"
    # The LinkedIn touch carries no email body and routes to the manual dispatcher.
    p = by_step[4]["payload"]
    assert "subject" not in p and "body" not in p
    assert by_step[4]["config_fingerprint"]["channel"] == "manual"


def test_enrolled_content_reaches_sender_end_to_end(monkeypatch):
    """enroll -> take touch-1 payload -> dispatch -> sender gets that copy."""
    captured = _enqueue_capture(monkeypatch)
    sequence_enrollment.enroll_contact(
        session=MagicMock(), client_id="client_a", contact_id=42,
        contact_email="owner@acme.com", first_name="Dana", company_name="Acme PM",
    )
    touch1_payload = next(c["payload"] for c in captured if c["payload"]["touch_step"] == 1)

    from src.services.work_orders.dispatchers import dispatch_email_touch

    order = SimpleNamespace(
        action_id="a1", client_id="client_a", entity_id="42", payload=touch1_payload,
    )

    class _RecordingSender:
        calls = []

        def send(self, **kwargs):
            _RecordingSender.calls.append(kwargs)
            from src.services.email_sender import SendResult
            return SendResult(message_id="<m@acme-out.com>")

    fake_session = MagicMock()
    fake_session.execute.return_value.fetchone.return_value = SimpleNamespace(
        contact_id=42, company_id="c", email="owner@acme.com",
    )

    class _Ctx:
        def __enter__(self):
            return fake_session

        def __exit__(self, *a):
            return False

    with (
        patch("src.core.database.get_db_context", lambda client_id=None: _Ctx()),
        patch("src.services.sequence_orchestrator.build_email_sender", return_value=_RecordingSender()),
        patch("src.services.sequence_orchestrator.evaluate_touch_gate",
              return_value=SimpleNamespace(ready=True, blocked_reasons=[])),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client",
              return_value=SimpleNamespace(mailbox_id=1, mailbox_address="s@acme-out.com", sending_domain="acme-out.com")),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True),
        patch("src.services.sequence_orchestrator.log_touch_dispatched"),
    ):
        receipt = dispatch_email_touch(order)

    assert receipt["outcome"] == "SENT"
    assert receipt.get("fail") is None
    sent = _RecordingSender.calls[-1]
    assert sent["subject"] == touch1_payload["subject"]
    assert sent["body"] == touch1_payload["body"]
    assert "placeholder" not in sent["body"].lower()
