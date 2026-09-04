from unittest.mock import MagicMock, patch

from migrations.apply_sandbox_dashboard_view import DDL


def test_view_ddl_does_not_use_security_invoker():
    """PR review fix: security_invoker made this view enforce RLS, which
    zeroed out every row for an external BI connection with no
    app.current_client_id ever set — the dashboard's only purpose. This
    view's WHERE clause is a fixed literal, not caller-influenced, so it is
    a deliberate, documented exception (see the module docstring)."""
    assert "security_invoker" not in DDL


def test_main_resets_security_invoker_explicitly_not_assumed():
    """CREATE OR REPLACE VIEW's handling of a previously-set reloption
    (security_invoker was set on the first deploy of this view, before this
    fix) should not be assumed — an explicit RESET makes the outcome
    certain on a server where the old version already ran."""
    with patch("migrations.apply_sandbox_dashboard_view.get_owner_db_context") as mock_ctx:
        db = MagicMock()
        mock_ctx.return_value.__enter__.return_value = db
        from migrations.apply_sandbox_dashboard_view import main

        main()

    executed = [str(c.args[0]) for c in db.execute.call_args_list if c.args]
    assert any("ALTER VIEW demo_sandbox_dashboard RESET (security_invoker)" in s for s in executed)
    assert any("GRANT SELECT ON demo_sandbox_dashboard" in s for s in executed)
