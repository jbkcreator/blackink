"""Unit tests for src.api.main's Group D / D-2 startup visibility check.

Before this, an inbound webhook route with a missing signing secret failed
closed SILENTLY — a per-request log.error plus a 403/406/503, no signal at
process startup. D-2 was exactly this trap: two settings held the same
Mailgun key for two different routers, so setting only one looked fine
until the other quietly rejected every delivery. This logs once, loudly,
at startup.
"""
from types import SimpleNamespace
from unittest.mock import patch

from src.api.main import _log_disabled_inbound_routes


def _settings(mailgun_signing_key=None, inbound_parse_secret=None):
    return SimpleNamespace(
        mailgun_signing_key=mailgun_signing_key,
        inbound_parse_secret=inbound_parse_secret,
    )


def test_logs_error_when_mailgun_key_unset():
    with patch("config.settings.get_settings", return_value=_settings(inbound_parse_secret="set")), \
         patch("src.api.main.logger") as mock_logger:
        _log_disabled_inbound_routes()
    mock_logger.error.assert_called_once()
    msg = str(mock_logger.error.call_args)
    assert "mailgun-inbound" in msg or "MAILGUN_SIGNING_KEY" in str(mock_logger.error.call_args.args)


def test_logs_error_when_inbound_parse_secret_unset():
    with patch("config.settings.get_settings", return_value=_settings(mailgun_signing_key="set")), \
         patch("src.api.main.logger") as mock_logger:
        _log_disabled_inbound_routes()
    mock_logger.error.assert_called_once()


def test_no_log_when_both_secrets_set():
    with patch("config.settings.get_settings", return_value=_settings(mailgun_signing_key="set", inbound_parse_secret="set")), \
         patch("src.api.main.logger") as mock_logger:
        _log_disabled_inbound_routes()
    mock_logger.error.assert_not_called()


def test_logs_both_when_both_unset():
    with patch("config.settings.get_settings", return_value=_settings()), \
         patch("src.api.main.logger") as mock_logger:
        _log_disabled_inbound_routes()
    mock_logger.error.assert_called_once()
    args = mock_logger.error.call_args.args
    assert args[1] == 2  # count of disabled routes
