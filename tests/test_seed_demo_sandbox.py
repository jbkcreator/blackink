from unittest.mock import MagicMock, patch

from src.tasks.seed_demo_sandbox import (
    SANDBOX_CLIENT_ID,
    build_mock_companies,
    build_mock_contacts_for_company,
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
    assert all(c["county_slug"] in {"hillsborough_fl", "pinellas_fl", "orange_fl", "miami_dade_fl"} for c in companies)
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
         patch("src.tasks.seed_demo_sandbox.log_event") as mock_log:
        session = MagicMock()
        mock_ctx.return_value.__enter__.return_value = session
        from src.tasks.seed_demo_sandbox import main
        main()
    executed_sql = "\n".join(str(c.args[0]) for c in session.execute.call_args_list if c.args)
    assert "ON CONFLICT" in executed_sql
    # 20 mock touches + 5 mock bookings, per the split doc's DoD.
    assert mock_log.call_count == 25
