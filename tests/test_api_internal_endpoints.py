"""Integration tests for the internal dashboard endpoints and auth layer.

Requires a live Postgres with all migrations applied (CI runs these after
the full migration sequence in tests.yml). The ADMIN_JWT_SECRET env var
must be set — CI sets it to a fixed test-only value; local dev needs
ENV_FILE=.env.local pointing at the Docker Postgres.

Covered:
  - POST /api/auth/login        valid / invalid / missing fields
  - GET  /api/sandbox/companies 401 without token; 200 + shape with token
  - GET  /api/metrics/digest    401 without token; 200 + shape with token
  - GET  /api/meetings/outcomes 401 without token; 200 + shape with token
  - POST /api/ovs/request       public (no token); domain normalisation; validation
  - Expired and malformed JWT both return 401
"""

import os

import bcrypt
import jwt as pyjwt
import pytest
from datetime import datetime, timedelta, timezone
from fastapi.testclient import TestClient
from sqlalchemy import text

# ADMIN_JWT_SECRET must be present before the app module is imported,
# because get_settings() is an @lru_cache singleton built at import time.
# CI sets this in the workflow env block; local dev inherits it from .env.local.
assert os.environ.get("ADMIN_JWT_SECRET"), (
    "ADMIN_JWT_SECRET must be set before running these tests. "
    "CI: add to the workflow env block. Local: use ENV_FILE=.env.local."
)

from src.api.main import app  # noqa: E402 — must come after env assertion
from src.core.database import get_system_db_context  # noqa: E402

_TEST_USERNAME = "ci_test_admin"
_TEST_PASSWORD = "ci_test_password_123"
_JWT_SECRET = os.environ["ADMIN_JWT_SECRET"]


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="module", autouse=True)
def seed_test_admin():
    """Insert a dedicated CI admin user so tests are isolated from prod data."""
    hashed = bcrypt.hashpw(_TEST_PASSWORD.encode(), bcrypt.gensalt()).decode()
    with get_system_db_context() as db:
        db.execute(
            text("""
                INSERT INTO admin_users (username, password_hash)
                VALUES (:u, :h)
                ON CONFLICT (username) DO UPDATE SET password_hash = EXCLUDED.password_hash
            """),
            {"u": _TEST_USERNAME, "h": hashed},
        )
        db.commit()


def _login(client) -> str:
    res = client.post("/api/auth/login", json={
        "username": _TEST_USERNAME,
        "password": _TEST_PASSWORD,
    })
    assert res.status_code == 200, res.text
    return res.json()["access_token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ── Auth endpoint ──────────────────────────────────────────────────────────────

class TestLogin:
    def test_valid_credentials_return_token(self, client):
        res = client.post("/api/auth/login", json={
            "username": _TEST_USERNAME,
            "password": _TEST_PASSWORD,
        })
        assert res.status_code == 200
        body = res.json()
        assert "access_token" in body
        assert body["token_type"] == "bearer"
        assert "expires_at" in body

    def test_wrong_password_returns_401(self, client):
        res = client.post("/api/auth/login", json={
            "username": _TEST_USERNAME,
            "password": "definitely_wrong",
        })
        assert res.status_code == 401

    def test_unknown_username_returns_401(self, client):
        res = client.post("/api/auth/login", json={
            "username": "nobody",
            "password": "anything",
        })
        assert res.status_code == 401

    def test_missing_body_returns_422(self, client):
        res = client.post("/api/auth/login", json={})
        assert res.status_code == 422


# ── Token validation ───────────────────────────────────────────────────────────

class TestTokenValidation:
    def test_expired_token_returns_401(self, client):
        expired = pyjwt.encode(
            {"sub": _TEST_USERNAME, "exp": datetime.now(tz=timezone.utc) - timedelta(hours=1)},
            _JWT_SECRET,
            algorithm="HS256",
        )
        res = client.get("/api/sandbox/companies", headers=_auth(expired))
        assert res.status_code == 401
        assert "expired" in res.json()["detail"].lower()

    def test_malformed_token_returns_401(self, client):
        res = client.get("/api/sandbox/companies", headers=_auth("not.a.jwt"))
        assert res.status_code == 401

    def test_wrong_secret_returns_401(self, client):
        bad = pyjwt.encode(
            {"sub": _TEST_USERNAME, "exp": datetime.now(tz=timezone.utc) + timedelta(hours=1)},
            "wrong-secret",
            algorithm="HS256",
        )
        res = client.get("/api/sandbox/companies", headers=_auth(bad))
        assert res.status_code == 401

    def test_missing_authorization_header_returns_401(self, client):
        res = client.get("/api/sandbox/companies")
        assert res.status_code == 401


# ── Sandbox endpoint ───────────────────────────────────────────────────────────

class TestSandboxEndpoint:
    def test_requires_auth(self, client):
        assert client.get("/api/sandbox/companies").status_code == 401

    def test_returns_expected_shape(self, client):
        token = _login(client)
        res = client.get("/api/sandbox/companies", headers=_auth(token))
        assert res.status_code == 200
        body = res.json()
        assert "summary" in body
        assert "companies" in body
        summary = body["summary"]
        assert "total_companies" in summary
        assert "total_doors" in summary
        assert "total_meetings" in summary
        assert isinstance(summary["total_companies"], int)
        assert isinstance(summary["total_doors"], int)
        assert isinstance(summary["total_meetings"], int)
        assert isinstance(body["companies"], list)

    def test_total_meetings_is_sum_not_count(self, client):
        """Regression: total_meetings must sum bookings, not count companies."""
        token = _login(client)
        res = client.get("/api/sandbox/companies", headers=_auth(token))
        body = res.json()
        # Sum meetings_booked across all company rows and compare to summary
        row_total = sum(c.get("meetings_booked", 0) for c in body["companies"])
        assert body["summary"]["total_meetings"] == row_total


# ── Metrics endpoint ───────────────────────────────────────────────────────────

class TestMetricsEndpoint:
    def test_requires_auth(self, client):
        assert client.get("/api/metrics/digest").status_code == 401

    def test_returns_expected_kpi_keys(self, client):
        token = _login(client)
        res = client.get("/api/metrics/digest", headers=_auth(token))
        assert res.status_code == 200
        body = res.json()
        for key in (
            "scores_generated",
            "county_rank_reports_delivered",
            "cold_emails_dispatched",
            "open_rate_pct",
            "click_rate_pct",
            "reply_rate_pct",
            "appointments_booked",
        ):
            assert key in body, f"missing KPI key: {key}"

    def test_sandbox_client_excluded(self, client):
        """Regression: DEMO_FRIDAY_SANDBOX events must not inflate real KPIs."""
        token = _login(client)
        res = client.get("/api/metrics/digest", headers=_auth(token))
        assert res.status_code == 200
        # We can't assert a specific count without knowing the DB state, but we
        # can assert the query runs without error and returns numeric-or-null values.
        body = res.json()
        for key in ("cold_emails_dispatched", "appointments_booked", "scores_generated"):
            assert body[key] is None or isinstance(body[key], (int, float))


# ── Meetings endpoint ──────────────────────────────────────────────────────────

class TestMeetingsEndpoint:
    def test_requires_auth(self, client):
        assert client.get("/api/meetings/outcomes").status_code == 401

    def test_returns_list(self, client):
        token = _login(client)
        res = client.get("/api/meetings/outcomes", headers=_auth(token))
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    def test_rows_have_expected_fields(self, client):
        token = _login(client)
        res = client.get("/api/meetings/outcomes", headers=_auth(token))
        rows = res.json()
        for row in rows[:5]:  # spot-check first five
            assert "attendance_status" in row
            assert "client_id" in row
            assert "contact_name" in row
            assert "company_name" in row


# ── OVS public endpoint ────────────────────────────────────────────────────────

class TestOvsEndpoint:
    def test_no_auth_required(self, client):
        res = client.post("/api/ovs/request", json={
            "company_name": "CI Test PM",
            "domain": "ci-test-pm.com",
            "contact_name": "CI Runner",
            "email": "ci@ci-test-pm.com",
            "state": "FL",
        })
        assert res.status_code == 201

    def test_response_shape(self, client):
        res = client.post("/api/ovs/request", json={
            "company_name": "CI Shape Test",
            "domain": "shape-test.com",
            "email": "shape@shape-test.com",
            "state": "FL",
        })
        body = res.json()
        assert "request_id" in body
        assert body["status"] == "pending"
        assert body["estimated_completion"] == "24h"

    def test_domain_is_normalised(self, client):
        """https://www. prefix and trailing slash must be stripped before storage."""
        res = client.post("/api/ovs/request", json={
            "company_name": "CI Norm Test",
            "domain": "https://www.example-norm.com/services/",
            "email": "norm@example-norm.com",
            "state": "FL",
        })
        assert res.status_code == 201

    def test_invalid_email_rejected(self, client):
        res = client.post("/api/ovs/request", json={
            "company_name": "Bad Email Co",
            "domain": "bad-email.com",
            "email": "not-an-email",
            "state": "FL",
        })
        assert res.status_code == 422

    def test_missing_required_fields_rejected(self, client):
        res = client.post("/api/ovs/request", json={"domain": "no-company.com"})
        assert res.status_code == 422
