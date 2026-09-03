"""Tests for the email sender seam — stub, real SMTP, and factory selection."""

from unittest.mock import MagicMock, patch

from src.services.email_sender import (
    SmtpEmailSender,
    StubEmailSender,
    build_email_sender,
)


def test_stub_mints_message_id_and_sends_nothing():
    r = StubEmailSender().send(
        from_address="rep@out.io", to_address="owner@acme.com",
        subject="Hi", body="Body", sending_domain="out.io",
    )
    assert r.message_id.startswith("<") and "out.io" in r.message_id


def test_smtp_sender_builds_and_transmits_message():
    with patch("src.services.email_sender.smtplib.SMTP") as mock_smtp:
        conn = mock_smtp.return_value.__enter__.return_value
        sender = SmtpEmailSender(
            host="smtp.gmail.com", port=587, password="app-pass",
            reply_to="client@theirfirm.com", bcc="bcc@blackink.io",
        )
        r = sender.send(
            from_address="rep@out.io", to_address="owner@acme.com",
            subject="Owner Visibility", body="Hello", sending_domain="out.io",
            in_reply_to="<prev@out.io>",
        )

    conn.starttls.assert_called_once()
    conn.login.assert_called_once_with("rep@out.io", "app-pass")  # login defaults to From
    # send_message called with the built message + explicit envelope rcpts (To + Bcc)
    args, kwargs = conn.send_message.call_args
    msg = args[0]
    assert kwargs["to_addrs"] == ["owner@acme.com", "bcc@blackink.io"]
    assert msg["Reply-To"] == "client@theirfirm.com"
    assert msg["In-Reply-To"] == "<prev@out.io>"
    assert msg["Message-ID"] == r.message_id
    assert msg["Bcc"] is None  # Bcc must NOT appear as a header


def test_build_email_sender_returns_stub_when_unconfigured():
    fake = MagicMock(smtp_host=None, smtp_password=None)
    with patch("config.settings.get_settings", return_value=fake):
        assert isinstance(build_email_sender(), StubEmailSender)


def test_build_email_sender_returns_smtp_when_configured():
    secret = MagicMock()
    secret.get_secret_value.return_value = "app-pass"
    fake = MagicMock(
        smtp_host="smtp.gmail.com", smtp_port=587, smtp_use_tls=True,
        smtp_username=None, smtp_password=secret,
        email_reply_to="c@x.com", email_bcc="b@y.com",
    )
    with patch("config.settings.get_settings", return_value=fake):
        assert isinstance(build_email_sender(), SmtpEmailSender)
