"""Finding #4: phone (DIAL_TASK) and LinkedIn (LINKEDIN_TASK) touches must be
surfaced and executable, not left QUEUED forever.

Two halves:
  - sequence_sweep now posts cards for all three touch classes, each to its
    own Slack channel (not just DISPATCH_EMAIL_TOUCH).
  - the manual dispatcher records completion for an APPROVED manual touch so
    the state machine closes — and never routes a phone/LinkedIn touch into
    the email sender.
"""

from types import SimpleNamespace

from src.tasks import sequence_sweep
from src.services.work_orders.dispatchers import (
    DISPATCHERS,
    dispatch_manual_task,
    _DEFER_OUTCOMES,
    _FAIL_OUTCOMES,
)


def _order(action_class, action_id="a1"):
    return SimpleNamespace(
        action_id=action_id, entity_id="1", action_class=action_class, slack_message_ts=None,
    )


def test_sweep_surfaces_all_three_touch_classes_to_their_channels(monkeypatch):
    orders = [
        _order("DISPATCH_EMAIL_TOUCH", "email1"),
        _order("DIAL_TASK", "dial1"),
        _order("LINKEDIN_TASK", "li1"),
    ]
    monkeypatch.setattr(sequence_sweep.wo, "due_batch", lambda client_id=None, limit=100: orders)

    routed = []

    async def _fake_post(order, channel_key):
        routed.append((order.action_class, channel_key))
        return True

    monkeypatch.setattr(sequence_sweep, "_post_due_card", _fake_post)

    posted = sequence_sweep.run_sweep()
    assert posted == 3
    assert dict(routed) == {
        "DISPATCH_EMAIL_TOUCH": "setter",
        "DIAL_TASK": "dial",
        "LINKEDIN_TASK": "setter",
    }


def test_manual_touch_registered_and_closes_without_send():
    """The 'manual' channel routes to a dispatcher that only records completion
    — no fail/defer flag, so an approved manual touch finalises as DONE."""
    assert "manual" in DISPATCHERS
    receipt = dispatch_manual_task(_order("DIAL_TASK"))
    assert receipt.get("fail") is None
    assert receipt.get("defer") is None
    assert receipt["dispatcher"] == "manual"


def test_manual_dispatcher_is_not_the_email_dispatcher():
    """A phone/LinkedIn touch must never reach the email send path."""
    assert DISPATCHERS["manual"] is dispatch_manual_task
    assert DISPATCHERS["manual"] is not DISPATCHERS["setter"]


def test_outcome_routing_sets_are_disjoint():
    assert _DEFER_OUTCOMES.isdisjoint(_FAIL_OUTCOMES)
