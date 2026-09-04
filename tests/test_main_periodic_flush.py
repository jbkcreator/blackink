"""Unit tests for src.api.main's periodic events-buffer flush loop — the
PR review fix for buffered events only ever being drained by the separate
daily_digest cron process, never by the long-running API process that
actually buffered them.
"""

import asyncio
from unittest.mock import patch

import pytest

from src.api.main import _periodic_flush_loop


async def _run_briefly(interval_seconds: float, duration_seconds: float) -> asyncio.Task:
    task = asyncio.create_task(_periodic_flush_loop(interval_seconds=interval_seconds))
    await asyncio.sleep(duration_seconds)
    return task


async def _cancel(task: asyncio.Task) -> None:
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


@pytest.mark.asyncio
async def test_periodic_flush_loop_calls_flush_pending_on_each_tick():
    with patch("src.api.main.flush_pending", return_value=2) as mock_flush:
        task = await _run_briefly(interval_seconds=0.01, duration_seconds=0.05)
        await _cancel(task)
    assert mock_flush.call_count >= 2


@pytest.mark.asyncio
async def test_periodic_flush_loop_survives_flush_pending_exception():
    with patch("src.api.main.flush_pending", side_effect=RuntimeError("db unreachable")):
        task = await _run_briefly(interval_seconds=0.01, duration_seconds=0.05)
        # The loop must still be running — a failed flush must not kill it.
        assert not task.done()
        await _cancel(task)


@pytest.mark.asyncio
async def test_periodic_flush_loop_stops_cleanly_on_cancel():
    with patch("src.api.main.flush_pending", return_value=0):
        task = asyncio.create_task(_periodic_flush_loop(interval_seconds=10))
        await asyncio.sleep(0)  # let it reach the first `await asyncio.sleep`
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
