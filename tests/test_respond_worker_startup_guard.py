"""Group D / D-10: the respond worker's process entrypoint must refuse to
start rather than silently classify every reply as NURTURE/ROUTED.

Before this guard, a missing `anthropic` SDK or an unset ANTHROPIC_API_KEY
made classifier.py's _fallback() fire for every message with no visible
error — the whole Respond product would no-op while every status column
read healthy. _assert_llm_configured() converts that into a loud startup
failure.

Deliberately does NOT gate Worker.__init__ / run_forever(): those are
exercised directly by tests/test_respond_worker_redis_recovery.py without
any anthropic/API-key setup, and gating construction would break that
existing test suite for no safety benefit — the check only matters at the
point the worker process actually starts (main()).
"""
from unittest.mock import patch

import pytest

from src.agents.respond.worker import _assert_llm_configured, main


def test_raises_when_anthropic_sdk_missing():
    with patch("src.agents.respond.classifier.anthropic", None):
        with pytest.raises(RuntimeError, match="anthropic"):
            _assert_llm_configured()


def test_raises_when_api_key_unset():
    from types import SimpleNamespace

    with patch("src.agents.respond.classifier.anthropic", object()), \
         patch("config.settings.get_settings", return_value=SimpleNamespace(anthropic_api_key=None)):
        with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
            _assert_llm_configured()


def test_passes_when_sdk_present_and_key_set():
    from types import SimpleNamespace

    with patch("src.agents.respond.classifier.anthropic", object()), \
         patch("config.settings.get_settings", return_value=SimpleNamespace(anthropic_api_key="sk-test")):
        _assert_llm_configured()  # must not raise


def test_main_refuses_to_start_worker_when_misconfigured():
    """main() must fail BEFORE constructing/running the Worker — the guard
    is worthless if the worker gets a chance to run first."""
    with patch("src.agents.respond.classifier.anthropic", None), \
         patch("src.agents.respond.worker.Worker") as MockWorker:
        with pytest.raises(RuntimeError):
            main()
        MockWorker.assert_not_called()
