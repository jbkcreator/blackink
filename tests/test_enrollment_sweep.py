"""Tests for enrollment_sweep — no live DB. Verifies the sweep hands eligible
contacts to enroll_contact, counts only real enrollments, passes personalization
fields, and never lets one contact's failure abort the batch."""
from types import SimpleNamespace
from unittest.mock import MagicMock, patch


def _row(contact_id, email="a@b.com", first_name="Pat", company_name="Acme PM"):
    return SimpleNamespace(
        contact_id=contact_id, email=email, first_name=first_name, company_name=company_name
    )


def _fake_ctx(rows):
    fake_session = MagicMock()
    fake_session.execute.return_value.fetchall.return_value = rows
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=fake_session)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, fake_session


class TestRunSweep:
    def test_enrolls_each_eligible_contact_and_counts_only_new_runs(self):
        rows = [_row(1), _row(2), _row(3)]
        ctx, _ = _fake_ctx(rows)

        with patch("src.tasks.enrollment_sweep.get_db_context", return_value=ctx), \
             patch("src.tasks.enrollment_sweep.enroll_contact",
                   side_effect=["run-1", None, "run-3"]) as mock_enroll:
            from src.tasks.enrollment_sweep import run_sweep
            enrolled = run_sweep("client-1")

        # Called once per candidate; None return (already enrolled) not counted.
        assert mock_enroll.call_count == 3
        assert enrolled == 2

    def test_passes_client_scope_and_personalization_fields(self):
        rows = [_row(7, email="owner@firm.com", first_name="Sam", company_name="Firm LLC")]
        ctx, fake_session = _fake_ctx(rows)

        with patch("src.tasks.enrollment_sweep.get_db_context", return_value=ctx) as mock_ctx, \
             patch("src.tasks.enrollment_sweep.enroll_contact", return_value="run-7") as mock_enroll:
            from src.tasks.enrollment_sweep import run_sweep
            run_sweep("client-9", limit=50)

        # RLS scope: get_db_context called with the client_id.
        assert mock_ctx.call_args.kwargs.get("client_id") == "client-9" \
            or mock_ctx.call_args.args == ("client-9",)
        # Candidate query is bound to the client + limit.
        params = fake_session.execute.call_args.args[1]
        assert params["client_id"] == "client-9"
        assert params["limit"] == 50
        # enroll_contact gets the session, ids, and personalization tags.
        args, kwargs = mock_enroll.call_args
        assert args[1] == "client-9"
        assert args[2] == 7
        assert args[3] == "owner@firm.com"
        assert kwargs["first_name"] == "Sam"
        assert kwargs["company_name"] == "Firm LLC"

    def test_one_contact_failure_does_not_abort_batch(self):
        rows = [_row(1), _row(2), _row(3)]
        ctx, _ = _fake_ctx(rows)

        with patch("src.tasks.enrollment_sweep.get_db_context", return_value=ctx), \
             patch("src.tasks.enrollment_sweep.enroll_contact",
                   side_effect=["run-1", RuntimeError("boom"), "run-3"]):
            from src.tasks.enrollment_sweep import run_sweep
            enrolled = run_sweep("client-1")

        # Contact 2 raised; 1 and 3 still enrolled.
        assert enrolled == 2

    def test_empty_candidate_set_returns_zero(self):
        ctx, _ = _fake_ctx([])
        with patch("src.tasks.enrollment_sweep.get_db_context", return_value=ctx), \
             patch("src.tasks.enrollment_sweep.enroll_contact") as mock_enroll:
            from src.tasks.enrollment_sweep import run_sweep
            enrolled = run_sweep("client-1")

        assert enrolled == 0
        mock_enroll.assert_not_called()


class TestEligibilitySql:
    def test_query_enforces_verified_optout_and_no_active_run(self):
        from src.tasks.enrollment_sweep import _ELIGIBLE_CONTACTS_SQL
        assert "email_status = 'VERIFIED'" in _ELIGIBLE_CONTACTS_SQL
        assert "is_opted_out = FALSE" in _ELIGIBLE_CONTACTS_SQL
        assert "owning_client_id = :client_id" in _ELIGIBLE_CONTACTS_SQL
        assert "NOT EXISTS" in _ELIGIBLE_CONTACTS_SQL
        assert "status = 'ACTIVE'" in _ELIGIBLE_CONTACTS_SQL


class TestAllClientsCron:
    def test_loops_active_clients_and_sums_enrolled(self):
        fake_session = MagicMock()
        fake_session.execute.return_value.all.return_value = [
            SimpleNamespace(client_id="c1"), SimpleNamespace(client_id="c2")
        ]
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_session)
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("scripts.cron_enrollment_sweep_all_clients.get_system_db_context", return_value=ctx), \
             patch("scripts.cron_enrollment_sweep_all_clients.run_sweep", side_effect=[3, 5]) as mock_sweep:
            from scripts.cron_enrollment_sweep_all_clients import main
            rc = main()

        assert rc == 0
        assert mock_sweep.call_count == 2

    def test_one_client_failure_sets_exit_code_but_continues(self):
        fake_session = MagicMock()
        fake_session.execute.return_value.all.return_value = [
            SimpleNamespace(client_id="c1"), SimpleNamespace(client_id="c2")
        ]
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_session)
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("scripts.cron_enrollment_sweep_all_clients.get_system_db_context", return_value=ctx), \
             patch("scripts.cron_enrollment_sweep_all_clients.run_sweep",
                   side_effect=[RuntimeError("boom"), 5]) as mock_sweep:
            from scripts.cron_enrollment_sweep_all_clients import main
            rc = main()

        assert rc == 1
        assert mock_sweep.call_count == 2
