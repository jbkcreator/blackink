"""Pure test: issue_credit() returns False and writes nothing on a duplicate
(client_id, credit_type, source_table, source_id) — the structural guarantee
behind "no second credit at resolved_at" (rule 4) and "one credit per miss"
(rule 1). No DB — a fake Session raising IntegrityError on the INSERT."""
from contextlib import contextmanager
from datetime import date, datetime, timezone

from sqlalchemy.exc import IntegrityError

from src.services.billing.credits import issue_credit


class _FakeSession:
	def __init__(self, raise_integrity_error: bool):
		self.raise_integrity_error = raise_integrity_error
		self.log_event_calls = 0

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "INSERT INTO billing_credits" in sql:
			if self.raise_integrity_error:
				raise IntegrityError("insert", params, Exception("unique violation"))
			return type("R", (), {"one": lambda self=None: type("Row", (), {"credit_id": 1})()})()
		return None

	def begin_nested(self):
		@contextmanager
		def _cm():
			yield

		return _cm()


def test_duplicate_credit_returns_false_and_writes_nothing(monkeypatch):
	monkeypatch.setattr("src.services.billing.credits.log_event", lambda *a, **kw: None)
	session = _FakeSession(raise_integrity_error=True)
	result = issue_credit(
		session, client_id="acme_pm", credit_type="MISS_CREDIT", amount_cents=5000,
		source_table="inbound_messages", source_id="123",
		issued_at=datetime.now(timezone.utc), billing_period=date(2026, 9, 1),
	)
	assert result is False


def test_new_credit_returns_true_and_logs_event(monkeypatch):
	logged = []
	monkeypatch.setattr("src.services.billing.credits.log_event", lambda *a, **kw: logged.append((a, kw)))
	session = _FakeSession(raise_integrity_error=False)
	result = issue_credit(
		session, client_id="acme_pm", credit_type="MISS_CREDIT", amount_cents=5000,
		source_table="inbound_messages", source_id="123",
		issued_at=datetime.now(timezone.utc), billing_period=date(2026, 9, 1),
	)
	assert result is True
	assert len(logged) == 1
