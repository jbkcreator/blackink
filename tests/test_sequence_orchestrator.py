"""Tests for sequence_orchestrator.dispatch_touch — sequencer core (post-approval send)."""

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.services.email_sender import SendResult
from src.services.sequence_orchestrator import dispatch_touch


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

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
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=_Sender())
    assert result.outcome == "COMPLIANCE_BLOCK"
    mock_mailbox.assert_not_called()
    mock_claim.assert_not_called()


# ---------------------------------------------------------------------------
# Cycle 2: no mailbox
# ---------------------------------------------------------------------------

def test_no_mailbox_returns_no_mailbox_outcome():
    from src.services.mailbox_dispatcher import NoMailboxAvailable
    session = MagicMock()
    with (
        patch("src.services.sequence_orchestrator.evaluate_touch_gate", return_value=_gate_result(True)),
        patch("src.services.sequence_orchestrator.get_active_mailbox_for_client", side_effect=NoMailboxAvailable("none")),
        patch("src.services.sequence_orchestrator.claim_touch") as mock_claim,
    ):
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=_Sender())
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
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=_Sender())
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
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=sender)
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
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, run_id="run-1", sender=sender)
    assert result.outcome == "SENT"
    assert result.message_id == "<abc@acme-out.com>"
    assert len(sender.calls) == 1
    mock_mark_sent.assert_called_once_with(session, "client_a", "dispatch-uuid", "<abc@acme-out.com>")
    mock_log.assert_called_once()


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
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=sender)
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
        result = dispatch_touch(session, _contact(), "client_a", touch_step=1, sender=sender)
    assert result.outcome == "RECLAIMED"
    mock_log.assert_not_called()
