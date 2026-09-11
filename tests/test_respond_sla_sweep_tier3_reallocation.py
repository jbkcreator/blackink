"""S-13 — SLA tier-3 backup-closer reallocation tests.

Covers: round-robin roster pick, fresh SLA window + escalation reset on a
real assignment, and the fail-closed fallback to the pre-S-13 terminal
REALLOCATED status when a client has no active roster configured.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

from src.tasks.respond_sla_sweep import _run_tier3, _pick_backup_closer


_AS_OF = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _sql_text_of_call(call) -> str:
    return str(call[0][0])


def _tier3_row(row_id=1, client_id="CL1"):
    return {
        "id": row_id, "client_id": client_id, "intent": "OBJECTION",
        "sender_email": "owner@co.com", "sender_name": "Owner Co",
    }


def test_pick_backup_closer_returns_none_when_no_roster_row():
    db = MagicMock()
    db.execute.return_value.mappings.return_value.first.return_value = None
    assert _pick_backup_closer(db, "CL1", _AS_OF) is None


def test_pick_backup_closer_returns_the_claimed_row():
    db = MagicMock()
    db.execute.return_value.mappings.return_value.first.return_value = {
        "slack_user_id": "U123", "display_name": "Jordan"
    }
    closer = _pick_backup_closer(db, "CL1", _AS_OF)
    assert closer == {"slack_user_id": "U123", "display_name": "Jordan"}


def test_tier3_with_active_roster_resets_escalation_and_starts_new_sla_window():
    db = MagicMock()
    row = _tier3_row()
    # First execute() call returns the tier3 SELECT rows; subsequent calls
    # (the roster pick, the UPDATE) just need to not error.
    db.execute.return_value.mappings.return_value.fetchall.return_value = [row]

    with patch("src.tasks.respond_sla_sweep._pick_backup_closer",
               return_value={"slack_user_id": "U123", "display_name": "Jordan"}), \
         patch("src.tasks.respond_sla_sweep.asyncio.run", return_value=None):
        fired = _run_tier3(db, _AS_OF)

    assert fired == 1
    update_call = next(
        c for c in db.execute.call_args_list
        if "escalation_level = 0" in _sql_text_of_call(c)
    )
    params = update_call[0][1]
    assert params["closer_id"] == "U123"
    assert params["new_sla"] == _AS_OF + timedelta(minutes=60)
    assert params["id"] == 1


def test_tier3_with_no_active_roster_falls_back_to_terminal_reallocated():
    db = MagicMock()
    row = _tier3_row()
    db.execute.return_value.mappings.return_value.fetchall.return_value = [row]

    with patch("src.tasks.respond_sla_sweep._pick_backup_closer", return_value=None), \
         patch("src.tasks.respond_sla_sweep.asyncio.run", return_value=None):
        fired = _run_tier3(db, _AS_OF)

    assert fired == 1
    update_call = next(
        c for c in db.execute.call_args_list
        if "REALLOCATED" in _sql_text_of_call(c) and "status" in _sql_text_of_call(c)
    )
    assert update_call[0][1]["id"] == 1


def test_tier3_alert_mentions_assigned_closer_when_present():
    db = MagicMock()
    row = _tier3_row()
    db.execute.return_value.mappings.return_value.fetchall.return_value = [row]
    posted = {}

    async def fake_run(coro):
        # side-effect-free stand-in; the real coroutine object is discarded
        return None

    with patch("src.tasks.respond_sla_sweep._pick_backup_closer",
               return_value={"slack_user_id": "U123", "display_name": "Jordan"}), \
         patch("src.tasks.respond_sla_sweep._alert_reallocated", new=AsyncMock()) as mock_alert, \
         patch("src.tasks.respond_sla_sweep.asyncio.run", side_effect=lambda coro: None):
        _run_tier3(db, _AS_OF)

    mock_alert.assert_called_once()
    _, closer_arg = mock_alert.call_args[0]
    assert closer_arg == {"slack_user_id": "U123", "display_name": "Jordan"}
