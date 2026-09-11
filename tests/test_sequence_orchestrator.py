"""Tests for sequence_orchestrator.dispatch_touch — sequencer core (post-approval send)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from src.services.email_sender import SendResult
from src.services.sequence_orchestrator import dispatch_touch


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _stub_unsubscribe():
    """Subtask 3.1.2's mandatory one-click unsubscribe footer/header is
    computed unconditionally on every send path that reaches sender.send()
    — stub it here rather than in every individual test, since
    email_unsubscribe.unsubscribe_url() needs EMAIL_UNSUBSCRIBE_SECRET
    configured, which this test suite deliberately never sets."""
    with (
        patch("src.services.sequence_orchestrator.unsubscribe_url", return_value="https://app.example.com/unsub?token=t"),
        patch("src.services.sequence_orchestrator.append_unsubscribe_footer", side_effect=lambda body, url: body),
        # S-8's tracking pixel — same reasoning as the unsubscribe stub
        # above: pixel_url() needs EMAIL_TRACKING_SECRET configured, which
        # this suite deliberately never sets.
        patch("src.services.sequence_orchestrator.pixel_url", return_value="https://app.example.com/pixel?token=p"),
        # Code-review fix (PR #50): dispatch_touch() now validates the
        # tracking secret before claim_touch() — stub it the same way as
        # pixel_url() above for every test except the ones specifically
        # testing this new check, which override it explicitly.
        patch("src.services.sequence_orchestrator.assert_tracking_configured"),
    ):
        yield

def _contact(contact_id=1, company_id="comp_abc", email="owner@acme.com"):
    return SimpleNamespace(contact_id=contact_id, company_id=company_id, email=email)


def _gate_result(ready: bool):
    return SimpleNamespace(ready=ready, blocked_reasons=[] if ready else ["opted_out"])


def _mailbox(mailbox_id=7, mailbox_address="sales@acme.com", sending_domain="acme-out.com"):
    return SimpleNamespace(
        mailbox_id=mailbox_id,
        mailbox_address=mailbox_address,
        sending_domain=sending_domain,
        instantly_account_email=None,
        client_id="client_a",
    )


class _Sender:
    def __init__(self, message_id="<m@acme-out.com>", raises=None):
        self.message_id = message_id
        self.raises = raises
        self.calls = []

    def send(self, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise self.raises
        return SendResult(message_id=self.message_id)


# ---------------------------------------------------------------------------
# Cycle 1: compliance block
# ---------------------------------------------------------------------------

def test_compliance_block_returns_compliance_block_outcome():
    session = MagicMock()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(False)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client") as mock_mailbox,
        patch("src.services.sequence_orchestrator.claim_touch") as mock_claim,
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=_Sender(), subject="S", body="B")
    assert result.outcome == "COMPLIANCE_BLOCK"
    mock_mailbox.assert_not_called()
    mock_claim.assert_not_called()


# ---------------------------------------------------------------------------
# Cycle 2: no mailbox
# ---------------------------------------------------------------------------

def test_missing_tracking_secret_raises_before_any_claim_is_made():
    """Code-review fix (Important, PR #50): a missing EMAIL_TRACKING_SECRET
    must fail BEFORE claim_touch() commits a SENDING row — previously
    pixel_url() only raised AFTER that commit, with no except around it,
    permanently stranding the dispatch (its UNIQUE claim blocks any retry).
    Proven here by asserting claim_touch is never even called."""
    session = MagicMock()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch(
            "src.services.sequence_orchestrator.assert_tracking_configured",
            side_effect=RuntimeError("Tracking tokens need EMAIL_TRACKING_SECRET configured"),
        ),
        patch("src.services.sequence_orchestrator.claim_touch") as mock_claim,
    ):
        with pytest.raises(RuntimeError, match="EMAIL_TRACKING_SECRET"):
            dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=_Sender(), subject="S", body="B")
    mock_claim.assert_not_called()
    session.commit.assert_not_called()


def test_no_mailbox_returns_no_mailbox_outcome():
    from src.services.mailbox_dispatcher import NoMailboxAvailable
    session = MagicMock()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", side_effect=NoMailboxAvailable("none")),
        patch("src.services.sequence_orchestrator.claim_touch") as mock_claim,
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=_Sender(), subject="S", body="B")
    assert result.outcome == "NO_MAILBOX"
    mock_claim.assert_not_called()


# ---------------------------------------------------------------------------
# Cycle 3: volume cap -> deferrable outcome
# ---------------------------------------------------------------------------

def test_all_mailboxes_capped_returns_volume_cap():
    from src.services.mailbox_dispatcher import AllMailboxesCapped
    session = MagicMock()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", side_effect=AllMailboxesCapped("capped")),
        patch("src.services.sequence_orchestrator.claim_touch") as mock_claim,
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=_Sender(), subject="S", body="B")
    assert result.outcome == "VOLUME_CAP"
    mock_claim.assert_not_called()


# ---------------------------------------------------------------------------
# Cycle 4: already claimed
# ---------------------------------------------------------------------------

def test_already_claimed_returns_already_claimed_and_does_not_send():
    session = MagicMock()
    sender = _Sender()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value=None),
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=sender, subject="S", body="B")
    assert result.outcome == "ALREADY_CLAIMED"
    assert sender.calls == []  # must not send a second time


# ---------------------------------------------------------------------------
# Cycle 5: happy path -> SENT, mark_sent called with message_id
# ---------------------------------------------------------------------------

def test_happy_path_sends_and_marks_sent():
    session = MagicMock()
    sender = _Sender(message_id="<abc@acme-out.com>")
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True) as mock_mark_sent,
        patch("src.services.sequence_orchestrator.log_touch_dispatched") as mock_log,
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, run_id="run-1", sender=sender, subject="S", body="B")
    assert result.outcome == "SENT"
    assert result.message_id == "<abc@acme-out.com>"
    assert len(sender.calls) == 1
    mock_mark_sent.assert_called_once_with(session, "client_a", "dispatch-uuid", "<abc@acme-out.com>")
    mock_log.assert_called_once()


def test_final_touch_completes_run_with_cooling():
    session = MagicMock()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True),
        patch("src.services.sequence_orchestrator.log_touch_dispatched"),
        patch("src.services.sequence_orchestrator.complete_run_with_cooling") as mock_cool,
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=5, run_id="run-1", sender=_Sender(), subject="S", body="B")
    assert result.outcome == "SENT"
    mock_cool.assert_called_once_with(session, "client_a", "run-1")


def test_non_final_touch_does_not_complete_run():
    session = MagicMock()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True),
        patch("src.services.sequence_orchestrator.log_touch_dispatched"),
        patch("src.services.sequence_orchestrator.complete_run_with_cooling") as mock_cool,
    ):
        dispatch_touch(session, _contact(), "client_a", touch_step=3, run_id="run-1", sender=_Sender(), subject="S", body="B")
    mock_cool.assert_not_called()


def test_send_failure_marks_failed_and_returns_send_failed():
    session = MagicMock()
    sender = _Sender(raises=RuntimeError("smtp down"))
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.mark_failed") as mock_mark_failed,
        patch("src.services.sequence_orchestrator.log_touch_dispatched") as mock_log,
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=sender, subject="S", body="B")
    assert result.outcome == "SEND_FAILED"
    mock_mark_failed.assert_called_once()
    mock_log.assert_not_called()  # no dispatch event on failure


def test_reclaimed_mid_flight_returns_reclaimed():
    session = MagicMock()
    sender = _Sender()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.mark_sent", return_value=False),
        patch("src.services.sequence_orchestrator.log_touch_dispatched") as mock_log,
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=sender, subject="S", body="B")
    assert result.outcome == "RECLAIMED"
    mock_log.assert_not_called()


# ---------------------------------------------------------------------------
# Touch-3 threading: In-Reply-To lookup
# ---------------------------------------------------------------------------

def test_touch3_passes_in_reply_to_from_touch1():
    """Touch 3 should look up Touch 1's message_id and pass it as in_reply_to."""
    session = MagicMock()
    sender = _Sender(message_id="<touch3@acme-out.com>")
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.get_touch_message_id", return_value="<touch1@acme-out.com>") as mock_get_mid,
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True),
        patch("src.services.sequence_orchestrator.log_touch_dispatched"),
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=3, run_id="run-1", sender=sender, subject="S", body="B")
    assert result.outcome == "SENT"
    mock_get_mid.assert_called_once_with(session, "run-1", 1)  # looks up touch_step=1
    assert sender.calls[0]["in_reply_to"] == "<touch1@acme-out.com>"


def test_touch1_does_not_look_up_in_reply_to():
    """Touch 1 is the first email — no threading lookup."""
    session = MagicMock()
    sender = _Sender()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.get_touch_message_id") as mock_get_mid,
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True),
        patch("src.services.sequence_orchestrator.log_touch_dispatched"),
    ):
        dispatch_touch(session, _contact(), "client_a", touch_step=1, run_id="run-1", sender=sender, subject="S", body="B")
    mock_get_mid.assert_not_called()
    assert sender.calls[0].get("in_reply_to") is None


def test_touch5_threads_to_touch3():
    """Touch 5 replies to Touch 3."""
    session = MagicMock()
    sender = _Sender()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.get_touch_message_id", return_value="<touch3@acme-out.com>") as mock_get_mid,
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True),
        patch("src.services.sequence_orchestrator.log_touch_dispatched"),
        patch("src.services.sequence_orchestrator.complete_run_with_cooling"),
    ):
        dispatch_touch(session, _contact(), "client_a", touch_step=5, run_id="run-1", sender=sender, subject="S", body="B")
    mock_get_mid.assert_called_once_with(session, "run-1", 3)  # looks up touch_step=3
    assert sender.calls[0]["in_reply_to"] == "<touch3@acme-out.com>"


# ---------------------------------------------------------------------------
# Finding #5: fail-closed on missing approved content — never send placeholder
# ---------------------------------------------------------------------------

def test_missing_content_returns_no_content_and_does_not_send():
    session = MagicMock()
    sender = _Sender()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch") as mock_claim,
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=sender)  # no subject/body
    assert result.outcome == "NO_CONTENT"
    assert sender.calls == []       # nothing transmitted
    mock_claim.assert_not_called()  # never even claims a slot


def test_partial_content_body_only_still_fails_closed():
    session = MagicMock()
    sender = _Sender()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch"),
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=sender, subject="", body="B")
    assert result.outcome == "NO_CONTENT"
    assert sender.calls == []


def test_supplied_content_is_sent_verbatim():
    session = MagicMock()
    sender = _Sender()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True),
        patch("src.services.sequence_orchestrator.log_touch_dispatched"),
    ):
        dispatch_touch(
            session, _contact(), "client_a", touch_step=1, run_id="run-1", sender=sender,
            subject="Approved subject", body="Approved body",
        )
    assert sender.calls[0]["subject"] == "Approved subject"
    assert sender.calls[0]["body"] == "Approved body"


def test_html_body_with_pixel_is_passed_to_sender():
    """S-8 — dispatch_touch must pass html_body= to sender.send() so a
    tracking pixel can be embedded; previously omitted entirely."""
    session = MagicMock()
    sender = _Sender()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", return_value="dispatch-uuid"),
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True),
        patch("src.services.sequence_orchestrator.log_touch_dispatched"),
    ):
        dispatch_touch(
            session, _contact(), "client_a", touch_step=1, run_id="run-1", sender=sender,
            subject="Approved subject", body="Approved body",
        )
    html_body = sender.calls[0]["html_body"]
    assert html_body is not None
    assert "https://app.example.com/pixel?token=p" in html_body
    assert "Approved body" in html_body


# ---------------------------------------------------------------------------
# Finding #1: the SENDING claim is committed BEFORE the external send, so a
# crash after send leaves a durable SENDING row (no rollback -> no re-send).
# ---------------------------------------------------------------------------

def test_claim_is_committed_before_send():
    order_of = []
    session = MagicMock()
    session.commit.side_effect = lambda: order_of.append("commit")

    class _RecordingSender:
        calls = []

        def send(self, **kwargs):
            order_of.append("send")
            return SendResult(message_id="<m@acme-out.com>")

    def _claim(*a, **k):
        order_of.append("claim")
        return "dispatch-uuid"

    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", return_value=_mailbox()),
        patch("src.services.sequence_orchestrator.claim_touch", side_effect=_claim),
        patch("src.services.sequence_orchestrator.mark_sent", return_value=True),
        patch("src.services.sequence_orchestrator.log_touch_dispatched"),
    ):
        result = dispatch_touch(
            session, _contact(), "client_a", touch_step=1, run_id="run-1",
            sender=_RecordingSender(), subject="S", body="B",
        )
    assert result.outcome == "SENT"
    # claim, then a commit, then send — the durability boundary.
    assert order_of[:3] == ["claim", "commit", "send"]
