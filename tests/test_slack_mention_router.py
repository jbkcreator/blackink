"""Unit tests for src.services.slack.listeners.handle_app_mention — the
@Blackink mention/intent router (audit item 3.0.3: no app_mention handler
existed at all; macro pipeline queries were only ever served by the
scheduled daily_digest.py cron). No live DB, no live Slack — calls the
plain async function directly, same convention as test_slack_listeners.py.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from src.services.slack.listeners import handle_app_mention


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def _mention_event(text: str, thread_ts=None, user="U_APPROVER"):
    event = {"text": f"<@U0BOTID> {text}", "ts": "1700000000.0001", "user": user}
    if thread_ts:
        event["thread_ts"] = thread_ts
    return event


@pytest.fixture(autouse=True)
def _authorized_by_default():
    """Code-review fix (Important, PR #50, finding 1): every test below
    exercises what an AUTHORIZED user sees — the dedicated rejection test
    overrides this explicitly. Without this check at all, any workspace
    member who could mention the bot could read platform-wide pipeline
    volume/engagement numbers and operational halt reasons/issuer
    identities — the same class of cross-tenant/sensitive data every other
    consequential action in this file already gates on approver_authorized()."""
    with patch("src.services.slack.listeners.approver_authorized", return_value=True):
        yield


@pytest.mark.parametrize("keyword", ["pipeline", "digest", "status", "numbers", "metrics", "PIPELINE"])
def test_digest_keywords_reuse_daily_digest_build_digest_text(keyword):
    """Reuses the same computation the 8am cron already posts — one
    implementation of the metrics, whether it's scheduled or on-demand."""
    say = AsyncMock()
    with patch("src.tasks.daily_digest.build_digest_text", return_value="DIGEST TEXT") as mock_build:
        _run(handle_app_mention(_mention_event(keyword), say))
    mock_build.assert_called_once()
    say.assert_awaited_once()
    assert say.await_args.kwargs["text"] == "DIGEST TEXT"


def test_digest_reply_threads_under_the_mention():
    say = AsyncMock()
    with patch("src.tasks.daily_digest.build_digest_text", return_value="DIGEST TEXT"):
        _run(handle_app_mention(_mention_event("pipeline"), say))
    assert say.await_args.kwargs["thread_ts"] == "1700000000.0001"


def test_digest_reply_uses_existing_thread_ts_when_mention_is_already_threaded():
    say = AsyncMock()
    with patch("src.tasks.daily_digest.build_digest_text", return_value="DIGEST TEXT"):
        _run(handle_app_mention(_mention_event("pipeline", thread_ts="1699999999.0000"), say))
    assert say.await_args.kwargs["thread_ts"] == "1699999999.0000"


def test_halt_status_with_no_active_halts():
    say = AsyncMock()
    with patch("src.services.slack.listeners.halt_service.get_active_halts", return_value=[]):
        _run(handle_app_mention(_mention_event("halt status"), say))
    say.assert_awaited_once()
    assert "No active halts" in say.await_args.kwargs["text"]


def test_halt_status_lists_active_halts():
    from types import SimpleNamespace

    say = AsyncMock()
    halt = SimpleNamespace(halt_id=7, scope="GLOBAL", scope_id=None, reason="incident", issued_by="slack:U1")
    with patch("src.services.slack.listeners.halt_service.get_active_halts", return_value=[halt]):
        _run(handle_app_mention(_mention_event("what halts are active"), say))
    text = say.await_args.kwargs["text"]
    assert "#7 GLOBAL" in text
    assert "incident" in text


def test_unrecognized_mention_gets_help_text():
    say = AsyncMock()
    _run(handle_app_mention(_mention_event("good morning!"), say))
    say.assert_awaited_once()
    assert "I understand" in say.await_args.kwargs["text"]


def test_bot_mention_prefix_is_stripped_before_keyword_matching():
    """Regression: Slack renders the mention itself as a literal <@U...>
    token at the start of event['text'] — matching against the raw text
    (which never contains a bare keyword, only the mention markup) would
    always fall through to the help text."""
    say = AsyncMock()
    with patch("src.tasks.daily_digest.build_digest_text", return_value="DIGEST TEXT"):
        _run(handle_app_mention({"text": "<@U0BOTID> digest", "ts": "1700000000.0002"}, say))
    assert say.await_args.kwargs["text"] == "DIGEST TEXT"


def test_mention_with_no_text_after_bot_id_gets_help():
    say = AsyncMock()
    _run(handle_app_mention({"text": "<@U0BOTID>", "ts": "1700000000.0003"}, say))
    assert "I understand" in say.await_args.kwargs["text"]


def test_unauthorized_user_is_rejected_before_any_digest_or_halt_query():
    """Code-review fix (Important, PR #50, finding 1): an unauthorized
    workspace member mentioning the bot must be rejected outright — never
    reach the digest computation or the halt-service query, regardless of
    which keyword they use."""
    say = AsyncMock()
    with patch("src.services.slack.listeners.approver_authorized", return_value=False), \
         patch("src.tasks.daily_digest.build_digest_text") as mock_build, \
         patch("src.services.slack.listeners.halt_service.get_active_halts") as mock_halts:
        _run(handle_app_mention(_mention_event("pipeline"), say))
    mock_build.assert_not_called()
    mock_halts.assert_not_called()
    say.assert_awaited_once()
    assert "not authorized" in say.await_args.kwargs["text"].lower()


def test_unauthorized_user_rejected_for_halt_query_too():
    say = AsyncMock()
    with patch("src.services.slack.listeners.approver_authorized", return_value=False), \
         patch("src.services.slack.listeners.halt_service.get_active_halts") as mock_halts:
        _run(handle_app_mention(_mention_event("halt status"), say))
    mock_halts.assert_not_called()
    assert "not authorized" in say.await_args.kwargs["text"].lower()
