"""Unit tests for src/api/winback_router.py's upload endpoint — mounted
standalone (not via src.api.main, which pulls in Slack/background-worker
setup that needs unrelated env config) with require_admin_jwt overridden,
FastAPI's standard dependency-override pattern. No live DB: the fix under
test (rejecting a zero-row CSV) runs entirely before the router ever opens
a database session, so these tests exercise the real code path with
nothing mocked out.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.deps import require_admin_jwt
from src.api.winback_router import router

app = FastAPI()
app.include_router(router)
app.dependency_overrides[require_admin_jwt] = lambda: {"sub": "test-admin"}

client = TestClient(app)


def _upload(csv_bytes: bytes):
	return client.post(
		"/api/v1/admin/winback/imports",
		params={"client_id": "acme_pm"},
		files={"file": ("owners.csv", csv_bytes, "text/csv")},
	)


def test_empty_file_rejected_with_400():
	resp = _upload(b"")
	assert resp.status_code == 400


def test_header_only_csv_rejected_with_400_before_any_import_created():
	# The exact PR-review finding: a file with bytes (a header row) but zero
	# data rows must never produce a "COMPLETED" import with total_rows=0.
	header_only = b"owner_name,property_address,county,phone,email\n"
	resp = _upload(header_only)
	assert resp.status_code == 400
	assert "no data rows" in resp.json()["detail"].lower()


def test_header_only_csv_with_trailing_blank_lines_still_rejected():
	header_only = b"owner_name,property_address,county,phone,email\n\n\n"
	resp = _upload(header_only)
	assert resp.status_code == 400
