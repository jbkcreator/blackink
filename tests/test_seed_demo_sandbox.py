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
         patch("src.tasks.seed_demo_sandbox.log_event") as mock_log:
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
         patch("src.tasks.seed_demo_sandbox.log_event") as mock_log:
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
         patch("src.tasks.seed_demo_sandbox.log_event"):
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
