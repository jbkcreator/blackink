"""Pure tests for the 60-day guarantee boundary (Subtask 1.2.3, rule 3):
3 qualifying sits -> override; 4 -> none; 5 -> none. No DB — a fake Session
standing in for the entitlement/count/update/insert statements."""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

from src.services.billing.guarantee import evaluate_sixty_day_guarantee


class _Row:
	def __init__(self, **kw):
		self.__dict__.update(kw)


class _FakeResult:
	def __init__(self, row):
		self._row = row

	def first(self):
		return self._row

	def one(self):
		return self._row

	def all(self):
		return [self._row] if self._row is not None else []


class _FakeSession:
	def __init__(self, entitlement_row, qualifying_count):
		self._entitlement_row = entitlement_row
		self._qualifying_count = qualifying_count
		self.inserted_overrides = []

	def execute(self, stmt, params=None):
		sql = str(stmt)
		if "FROM client_entitlements" in sql and "SELECT entitlement_id" in sql:
			return _FakeResult(self._entitlement_row)
		if "FROM appointments" in sql:
			return _FakeResult(_Row(n=self._qualifying_count))
		if "UPDATE client_entitlements" in sql:
			return _FakeResult(None)
		if "INSERT INTO subscription_overrides" in sql:
			self.inserted_overrides.append(params)
			return _FakeResult(None)
		return _FakeResult(None)

	def begin_nested(self):
		@contextmanager
		def _cm():
			yield

		return _cm()


def _entitlement(activated_at):
	return _Row(entitlement_id=1, offer_code="owner_growth", activated_at=activated_at, guarantee_applied=False)


def test_three_qualifying_sits_applies_override(monkeypatch):
	activated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
	as_of = activated_at + timedelta(days=61)
	session = _FakeSession(_entitlement(activated_at), qualifying_count=3)
	import src.services.billing.guarantee as mod

	monkeypatch.setattr(mod, "log_event", lambda *a, **kw: None)
	result = evaluate_sixty_day_guarantee(session, client_id="acme_pm", as_of=as_of)
	assert result is True
	assert len(session.inserted_overrides) == 1


def test_four_qualifying_sits_applies_no_override(monkeypatch):
	activated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
	as_of = activated_at + timedelta(days=61)
	session = _FakeSession(_entitlement(activated_at), qualifying_count=4)
	import src.services.billing.guarantee as mod

	monkeypatch.setattr(mod, "log_event", lambda *a, **kw: None)
	result = evaluate_sixty_day_guarantee(session, client_id="acme_pm", as_of=as_of)
	assert result is False
	assert session.inserted_overrides == []


def test_five_qualifying_sits_applies_no_override(monkeypatch):
	activated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
	as_of = activated_at + timedelta(days=61)
	session = _FakeSession(_entitlement(activated_at), qualifying_count=5)
	import src.services.billing.guarantee as mod

	monkeypatch.setattr(mod, "log_event", lambda *a, **kw: None)
	result = evaluate_sixty_day_guarantee(session, client_id="acme_pm", as_of=as_of)
	assert result is False
	assert session.inserted_overrides == []


def test_before_day_sixty_is_a_no_op(monkeypatch):
	activated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
	as_of = activated_at + timedelta(days=30)
	session = _FakeSession(_entitlement(activated_at), qualifying_count=0)
	import src.services.billing.guarantee as mod

	monkeypatch.setattr(mod, "log_event", lambda *a, **kw: None)
	result = evaluate_sixty_day_guarantee(session, client_id="acme_pm", as_of=as_of)
	assert result is False
	assert session.inserted_overrides == []
