from unittest.mock import MagicMock, patch

from src.tasks.seed_demo_sandbox import (
    SANDBOX_CLIENT_ID,
    build_mock_companies,
    build_mock_contacts_for_company,
    export_dashboard_to_sheet,
)


def test_sandbox_client_id_matches_blueprint():
    assert SANDBOX_CLIENT_ID == "DEMO_FRIDAY_SANDBOX"


def test_builds_enough_companies_to_exceed_the_50_contact_requirement():
    """The split doc's DoD is '50 mock CONTACTS', not 50 companies — and
    every company yields exactly 2 contacts, so >=25 companies satisfies
    it. Asserted in terms of the real requirement (contacts) so this test
    can't drift from the DoD it exists to protect."""
    companies = build_mock_companies()
    assert len(companies) * 2 >= 50
    names = [c["company_name"] for c in companies]
    assert not any(n.lower().startswith("test company") for n in names)
    assert len(set(c["domain"] for c in companies)) == len(companies)  # domain is UNIQUE in companies
    assert all(c["county_slug"] in {"hillsborough_fl", "pinellas_fl"} for c in companies)
    # v2 spec correction: sandbox scope narrowed from 4 counties to 2.
    assert not any(c["county_slug"] in {"orange_fl", "miami_dade_fl"} for c in companies)
    assert all(15 <= c["door_count_est"] <= 400 for c in companies)


def test_company_generation_is_deterministic_across_calls():
    """Idempotency of the seeder depends on re-runs producing the SAME
    door counts — otherwise every run rewrites every row with new numbers.
    A module-level random.seed() does NOT give this (it is consumed once at
    import); a per-call seeded Random instance does."""
    assert build_mock_companies() == build_mock_companies()


def test_each_company_gets_exactly_two_role_distinct_contacts():
    company = build_mock_companies()[0]
    contacts = build_mock_contacts_for_company(company)
    assert len(contacts) == 2
    roles = {c["contact_role_type"] for c in contacts}
    assert roles == {"OWNER_BROKER_MD", "OFFICE_MANAGER_OPS"}
    assert len(set(c["email"] for c in contacts)) == 2  # contacts.email has a UNIQUE index


def test_main_is_idempotent_upsert_not_duplicate_insert():
    with patch("src.tasks.seed_demo_sandbox.get_system_db_context") as mock_ctx, \
         patch("src.tasks.seed_demo_sandbox.log_event") as mock_log, \
         patch("src.tasks.seed_demo_sandbox.export_dashboard_to_sheet", return_value=0):
        session = MagicMock()
        mock_ctx.return_value.__enter__.return_value = session
        # No existing owner for any company — the collision check must not
        # treat a bare MagicMock()'s auto-mocked (truthy) return as a real
        # collision, so every company proceeds through the normal upsert path.
        session.execute.return_value.mappings.return_value.first.return_value = None
        from src.tasks.seed_demo_sandbox import main
        main()
    executed_sql = "\n".join(str(c.args[0]) for c in session.execute.call_args_list if c.args)
    assert "ON CONFLICT" in executed_sql
    # 20 mock touches + 5 mock bookings, per the split doc's DoD.
    assert mock_log.call_count == 25


def test_main_skips_company_and_contacts_already_owned_by_another_client():
    """company_id = sha256(domain) is globally unique — if a seed domain
    collides with a REAL customer's company, the seeder must not reassign
    that company (or overwrite its contacts) to the demo tenant. This is
    the fix for the PR review finding: silently upserting owning_client_id
    on any conflict is a cross-tenant data-integrity bug."""
    colliding = build_mock_companies()[0]
    colliding_id = colliding["company_id"]

    def fake_execute(_clause, params=None):
        result = MagicMock()
        if params and params.get("company_id") == colliding_id:
            result.mappings.return_value.first.return_value = {"owning_client_id": "some_other_real_client"}
        else:
            result.mappings.return_value.first.return_value = None
        return result

    with patch("src.tasks.seed_demo_sandbox.get_system_db_context") as mock_ctx, \
         patch("src.tasks.seed_demo_sandbox.log_event") as mock_log, \
         patch("src.tasks.seed_demo_sandbox.export_dashboard_to_sheet", return_value=0):
        session = MagicMock()
        session.execute.side_effect = fake_execute
        mock_ctx.return_value.__enter__.return_value = session
        from src.tasks.seed_demo_sandbox import main
        main()

    for call in session.execute.call_args_list:
        if not call.args:
            continue
        stmt = str(call.args[0])
        params = call.args[1] if len(call.args) > 1 else None
        if "INSERT INTO companies" in stmt or "INSERT INTO contacts" in stmt:
            assert not (params and params.get("company_id") == colliding_id), (
                f"seeder wrote to a collided company_id it does not own: {stmt}"
            )

    # The 39 non-colliding companies still seed their events; the colliding
    # company's fake events must never be logged against a real customer's row.
    for call in mock_log.call_args_list:
        assert call.kwargs.get("entity_id") != colliding_id


def test_main_reassigns_county_for_already_seeded_sandbox_company():
    """PR review finding: the v2 county-narrowing fix (4 counties -> 2) only
    changes what build_mock_companies() generates going forward. A company
    already seeded under the OLD 4-county list keeps its stale county_slug
    forever, because ON CONFLICT previously updated door_count_est/
    current_pm_software/owning_client_id but never county_slug. Simulates a
    sandbox-owned company already sitting in orange_fl (pre-v2 data) and
    confirms a re-run reassigns it to the new 2-county set."""
    target = build_mock_companies()[0]
    target_id = target["company_id"]

    def fake_execute(_clause, params=None):
        result = MagicMock()
        if params and params.get("company_id") == target_id:
            # Sandbox already owns this row -> not a collision -> upsert proceeds.
            result.mappings.return_value.first.return_value = {"owning_client_id": SANDBOX_CLIENT_ID}
        else:
            result.mappings.return_value.first.return_value = None
        return result

    with patch("src.tasks.seed_demo_sandbox.get_system_db_context") as mock_ctx, \
         patch("src.tasks.seed_demo_sandbox.log_event"), \
         patch("src.tasks.seed_demo_sandbox.export_dashboard_to_sheet", return_value=0):
        session = MagicMock()
        session.execute.side_effect = fake_execute
        mock_ctx.return_value.__enter__.return_value = session
        from src.tasks.seed_demo_sandbox import main
        main()

    upsert_call = next(
        call for call in session.execute.call_args_list
        if call.args and "INSERT INTO companies" in str(call.args[0])
        and call.args[1].get("company_id") == target_id
    )
    assert "county_slug = EXCLUDED.county_slug" in str(upsert_call.args[0])
    assert upsert_call.args[1]["county_slug"] in {"hillsborough_fl", "pinellas_fl"}


def test_export_dashboard_to_sheet_skips_when_unconfigured():
    """Looker Studio reads a Google Sheet, not Postgres directly (avoids
    exposing the shared production database to the internet). Without
    credentials configured, export must no-op rather than error — this is
    the default state for every environment that hasn't set up the Sheets
    export yet (e.g. CI, a fresh dev machine)."""
    fake_settings = MagicMock(google_sheets_credentials_path=None, google_sheets_sandbox_id=None)
    with patch("src.tasks.seed_demo_sandbox.get_settings", return_value=fake_settings), \
         patch("src.tasks.seed_demo_sandbox.get_system_db_context") as mock_ctx, \
         patch("src.tasks.seed_demo_sandbox.gspread") as mock_gspread:
        assert export_dashboard_to_sheet() == 0
    mock_ctx.assert_not_called()
    mock_gspread.service_account.assert_not_called()


def test_export_dashboard_to_sheet_writes_header_and_rows():
    fake_settings = MagicMock(
        google_sheets_credentials_path="secrets/fake.json",
        google_sheets_sandbox_id="fake-sheet-id",
    )
    fake_rows = [("Sunbelt Property Management", "hillsborough_fl", 120, "AppFolio", "CLIENT", 2, 1)]

    with patch("src.tasks.seed_demo_sandbox.get_settings", return_value=fake_settings), \
         patch("src.tasks.seed_demo_sandbox.get_system_db_context") as mock_ctx, \
         patch("src.tasks.seed_demo_sandbox.gspread") as mock_gspread:
        session = MagicMock()
        session.execute.return_value.fetchall.return_value = fake_rows
        mock_ctx.return_value.__enter__.return_value = session
        worksheet = MagicMock()
        mock_gspread.service_account.return_value.open_by_key.return_value.sheet1 = worksheet

        result = export_dashboard_to_sheet()

    mock_gspread.service_account.assert_called_once_with(filename="secrets/fake.json")
    mock_gspread.service_account.return_value.open_by_key.assert_called_once_with("fake-sheet-id")
    worksheet.clear.assert_called_once()
    written = worksheet.update.call_args[0][0]
    assert written[0] == [
        "company_name", "county_slug", "door_count_est", "current_pm_software",
        "status", "touches_sent", "meetings_booked",
    ]
    assert written[1] == list(fake_rows[0])
    assert result == 1


def test_export_dashboard_to_sheet_failure_does_not_raise():
    """A Sheets-side failure (bad credentials, network blip, API quota) must
    never fail the seeding run that calls this — seeding the database is
    the important part; updating the demo dashboard sheet is secondary."""
    fake_settings = MagicMock(
        google_sheets_credentials_path="secrets/fake.json",
        google_sheets_sandbox_id="fake-sheet-id",
    )
    with patch("src.tasks.seed_demo_sandbox.get_settings", return_value=fake_settings), \
         patch("src.tasks.seed_demo_sandbox.get_system_db_context") as mock_ctx, \
         patch("src.tasks.seed_demo_sandbox.gspread") as mock_gspread:
        session = MagicMock()
        session.execute.return_value.fetchall.return_value = []
        mock_ctx.return_value.__enter__.return_value = session
        mock_gspread.service_account.side_effect = RuntimeError("auth failed")

        assert export_dashboard_to_sheet() == 0
