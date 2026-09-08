"""Tests for sequence_halt — global opt-out service (Task 3.1.3 / ticket 27).

Uses the FakeSession pattern (MagicMock) — no live DB required.
Tests external behavior: the SQL statements issued and the correct
result handling. Does NOT test SQLAlchemy internals.
"""

from unittest.mock import MagicMock, call, patch

import pytest

from src.services.sequence_halt import halt_sequence_for_contact


# ── FakeSession helpers ──────────────────────────────────────────────────────


def _make_session(run_rows=None):
    """Build a mock session that returns the given run rows from the
    UPDATE ... RETURNING call, and no-ops for everything else."""
    session = MagicMock()

    # The UPDATE contacts execute returns a trivial result
    contacts_result = MagicMock()
    contacts_result.rowcount = 1

    # The UPDATE sequence_runs execute returns run_rows (list of dicts)
    runs_result = MagicMock()
    if run_rows is None:
        run_rows = []
    runs_result.mappings.return_value.all.return_value = run_rows

    # The UPDATE agent_work_orders execute returns a trivial result
    orders_result = MagicMock()
    orders_result.rowcount = len(run_rows)  # one cancel per run

    # Wire up side_effect: first call → contacts, second → runs,
    # subsequent calls → order cancellations.
    call_results = [contacts_result, runs_result] + [orders_result] * len(run_rows)
    session.execute.side_effect = call_results
    return session


# ── Tests ────────────────────────────────────────────────────────────────────


def test_halt_updates_contact_is_opted_out():
    """halt_sequence_for_contact issues an UPDATE contacts SET is_opted_out = TRUE."""
    session = _make_session(run_rows=[])

    with patch("src.services.sequence_halt.get_system_db_context") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        halt_sequence_for_contact(contact_id=42)

    # First execute call should be the contacts UPDATE
    first_sql = str(session.execute.call_args_list[0][0][0])
    assert "contacts" in first_sql.lower()
    assert "is_opted_out" in first_sql
    params = session.execute.call_args_list[0][0][1]
    assert params["contact_id"] == 42


def test_halt_sets_run_status_halted():
    """halt_sequence_for_contact issues UPDATE sequence_runs SET status='HALTED'."""
    session = _make_session(run_rows=[])

    with patch("src.services.sequence_halt.get_system_db_context") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        halt_sequence_for_contact(contact_id=99)

    # Second execute call should be the sequence_runs UPDATE
    second_sql = str(session.execute.call_args_list[1][0][0])
    assert "sequence_runs" in second_sql.lower()
    assert "HALTED" in second_sql


def test_halt_cancels_pending_work_orders_for_active_run():
    """When an ACTIVE run exists, halt_sequence_for_contact cancels its
    QUEUED/SNOOZED work orders."""
    run_row = {"run_id": "deadbeef-dead-dead-dead-deadbeef0001", "client_id": "client-abc"}
    session = _make_session(run_rows=[run_row])

    with patch("src.services.sequence_halt.get_system_db_context") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        halt_sequence_for_contact(contact_id=5)

    # Third execute = agent_work_orders cancellation
    assert session.execute.call_count == 3
    third_sql = str(session.execute.call_args_list[2][0][0])
    assert "agent_work_orders" in third_sql.lower()
    assert "CANCELLED" in third_sql
    third_params = session.execute.call_args_list[2][0][1]
    assert third_params["client_id"] == "client-abc"
    assert third_params["run_id"] == run_row["run_id"]


def test_halt_no_run_skips_work_order_cancel():
    """When there is no ACTIVE run, no agent_work_orders UPDATE is issued."""
    session = _make_session(run_rows=[])

    with patch("src.services.sequence_halt.get_system_db_context") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        halt_sequence_for_contact(contact_id=7)

    # Only two executes: contacts + sequence_runs (no run to cancel)
    assert session.execute.call_count == 2


def test_halt_commits_session():
    """halt_sequence_for_contact always calls session.commit()."""
    session = _make_session(run_rows=[])

    with patch("src.services.sequence_halt.get_system_db_context") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        halt_sequence_for_contact(contact_id=1)

    session.commit.assert_called_once()


def test_halt_multiple_runs_cancels_each():
    """Multiple ACTIVE runs (shouldn't happen in practice due to the partial
    unique index, but the code handles it gracefully)."""
    run_rows = [
        {"run_id": "run-1111-1111-1111-111111111111", "client_id": "client-a"},
        {"run_id": "run-2222-2222-2222-222222222222", "client_id": "client-b"},
    ]
    session = _make_session(run_rows=run_rows)

    with patch("src.services.sequence_halt.get_system_db_context") as mock_ctx:
        mock_ctx.return_value.__enter__ = lambda s: session
        mock_ctx.return_value.__exit__ = MagicMock(return_value=False)
        halt_sequence_for_contact(contact_id=10)

    # 2 contacts + 1 runs + 2 work-order cancels = 4 executes
    assert session.execute.call_count == 4
