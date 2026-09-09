"""Pure test: issue_credit() returns False and writes nothing on a genuine
duplicate (client_id, credit_type, source_table, source_id) — the structural
guarantee behind "no second credit at resolved_at" (rule 4) and "one credit
per miss" (rule 1). Detected via `ON CONFLICT ... DO NOTHING RETURNING`
(returns no row), never by catching IntegrityError — a real constraint
violation (FK, CHECK, etc.) must propagate and be retried, not be silently
mistaken for "already credited" (PR #37 review finding).

No DB — a fake Session standing in for the INSERT."""
from datetime import date, datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from src.services.billing.credits import issue_credit


class _FakeSession:
	def __init__(self, *, conflict: bool = False, raise_error: Exception | None = None):
		self.conflict = conflict
		self.raise_error = raise_error

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "INSERT INTO billing_credits" in sql:
			if self.raise_error is not None:
				raise self.raise_error
			if self.conflict:
				return type("R", (), {"first": lambda self=None: None})()
			return type("R", (), {"first": lambda self=None: type("Row", (), {"credit_id": 1})()})()
		return None


def test_duplicate_credit_returns_false_and_writes_nothing(monkeypatch):
	monkeypatch.setattr("src.services.billing.credits.log_event", lambda *a, **kw: None)
	session = _FakeSession(conflict=True)
	result = issue_credit(
		session, client_id="acme_pm", credit_type="MISS_CREDIT", amount_cents=5000,
		source_table="inbound_messages", source_id="123",
		issued_at=datetime.now(timezone.utc), billing_period=date(2026, 9, 1),
	)
	assert result is False


def test_new_credit_returns_true_and_logs_event(monkeypatch):
	logged = []
	monkeypatch.setattr("src.services.billing.credits.log_event", lambda *a, **kw: logged.append((a, kw)))
	session = _FakeSession(conflict=False)
	result = issue_credit(
		session, client_id="acme_pm", credit_type="MISS_CREDIT", amount_cents=5000,
		source_table="inbound_messages", source_id="123",
		issued_at=datetime.now(timezone.utc), billing_period=date(2026, 9, 1),
	)
	assert result is True
	assert len(logged) == 1


def test_non_duplicate_db_error_propagates_and_is_not_swallowed():
	"""PR #37 review finding — a FK/CHECK/other IntegrityError must NOT be
	mistaken for a duplicate-credit no-op. issue_credit() no longer catches
	IntegrityError at all (ON CONFLICT DO NOTHING RETURNING handles the one
	case that IS a legitimate no-op), so any other DB error raised by the
	INSERT must propagate to the caller for a real retry."""
	session = _FakeSession(raise_error=IntegrityError("insert", {}, Exception("foreign key violation")))
	with pytest.raises(IntegrityError):
		issue_credit(
			session, client_id="acme_pm", credit_type="MISS_CREDIT", amount_cents=5000,
			source_table="inbound_messages", source_id="123",
			issued_at=datetime.now(timezone.utc), billing_period=date(2026, 9, 1),
		)
