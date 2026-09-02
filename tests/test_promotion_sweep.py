"""Domain-normalization and contact-completeness coverage for the
promotion sweep pipeline (Week 1 Subtasks 1.1.2 and 1.1.3). Pure unit
tests — no live database required.

The outcome-counting / dedup-event / contact-promotion logic in
src/tasks/promotion_sweep.py itself is exercised end-to-end against a real
Postgres in tests/test_tenant_isolation.py (it drives Company/Contact ORM
inserts and raw SQL text() queries together, which isn't worth
over-mocking here).
"""

from types import SimpleNamespace

import pytest

from src.loaders.base import BaseIngestLoader
from src.tasks.promotion_sweep import _contacts_are_complete


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("www.TestPM.COM/", "testpm.com"),
        ("testpm.com", "testpm.com"),
        ("testpm.com/", "testpm.com"),
        ("TESTPM.COM", "testpm.com"),
        ("https://www.SuncoastPM.com/", "suncoastpm.com"),
        ("www.suncoastpm.com", "suncoastpm.com"),
        ("suncoastpm.com.", "suncoastpm.com"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_domain(raw, expected):
    assert BaseIngestLoader.normalize_domain(raw) == expected


def test_normalize_domain_makes_www_and_bare_variants_identical():
    assert BaseIngestLoader.normalize_domain("www.TestPM.COM/") == BaseIngestLoader.normalize_domain("testpm.com")


def _raw_contact(role, **overrides):
    fields = dict(
        role=role,
        first_name="Pat",
        last_name="Smith",
        title="Managing Broker",
        email="pat@testpm.com",
        phone="+15551234567",
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_contacts_are_complete_when_both_roles_fully_populated():
    by_role = {
        "OWNER_BROKER_MD": _raw_contact("OWNER_BROKER_MD"),
        "OFFICE_MANAGER_OPS": _raw_contact("OFFICE_MANAGER_OPS", email="ops@testpm.com"),
    }
    assert _contacts_are_complete(by_role) is True


def test_contacts_are_complete_false_when_a_role_is_missing():
    by_role = {"OWNER_BROKER_MD": _raw_contact("OWNER_BROKER_MD")}
    assert _contacts_are_complete(by_role) is False


@pytest.mark.parametrize("missing_field", ["first_name", "last_name", "title", "email", "phone"])
def test_contacts_are_complete_false_when_either_contact_missing_a_required_field(missing_field):
    by_role = {
        "OWNER_BROKER_MD": _raw_contact("OWNER_BROKER_MD", **{missing_field: None}),
        "OFFICE_MANAGER_OPS": _raw_contact("OFFICE_MANAGER_OPS", email="ops@testpm.com"),
    }
    assert _contacts_are_complete(by_role) is False
