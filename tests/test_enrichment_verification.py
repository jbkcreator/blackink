"""Tests for src/tasks/enrichment_verification.py (Subtask 3.2.1). No live
DB — mirrors tests/test_winback_ingest.py's scripted-session style."""

from datetime import datetime, timezone

from src.services.owner_enrichment import StubOwnerEnrichmentProvider, TracerfyEnrichmentProvider
from src.tasks.enrichment_verification import (
	_bump_attempts,
	_count_pending_backlog,
	_provider_name,
	_self_heal_exhausted_rows,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_provider_name_stub():
	assert _provider_name(StubOwnerEnrichmentProvider()) == "stub"


def test_provider_name_tracerfy():
	assert _provider_name(TracerfyEnrichmentProvider("fake-key")) == "tracerfy"


class _SelfHealFakeSession:
	"""Scripts the self-heal UPDATE...RETURNING, then swallows log_event's
	own INSERT INTO events (real log_event runs against this fake — no
	live DB, so its own execute() call just needs to not raise)."""

	def __init__(self, returned_rows):
		self._returned_rows = returned_rows
		self.calls: list = []

	def execute(self, stmt, params=None):
		sql = str(stmt)
		self.calls.append(sql)
		if "RETURNING winback_row_id" in sql:
			return _Rows(self._returned_rows)
		return _Rows([])


class _Row:
	def __init__(self, winback_row_id, enrichment_provider):
		self.winback_row_id = winback_row_id
		self.enrichment_provider = enrichment_provider


class _Rows:
	def __init__(self, rows):
		self._rows = rows

	def fetchall(self):
		return self._rows


def test_self_heal_marks_and_logs_zero_rows_when_nothing_exhausted():
	session = _SelfHealFakeSession([])
	count = _self_heal_exhausted_rows(session, "acme_pm", max_attempts=3, now=_NOW)
	assert count == 0


def test_self_heal_returns_count_of_exhausted_rows():
	session = _SelfHealFakeSession([_Row(1, "tracerfy (attempts_exhausted)"), _Row(2, "tracerfy (attempts_exhausted)")])
	count = _self_heal_exhausted_rows(session, "acme_pm", max_attempts=3, now=_NOW)
	assert count == 2
	# one owner_enrichment_completed INSERT per exhausted row
	insert_calls = [c for c in session.calls if "INSERT INTO events" in c]
	assert len(insert_calls) == 2


class _BumpFakeSession:
	def __init__(self):
		self.params = None

	def execute(self, stmt, params=None):
		self.params = params
		return None


def test_bump_attempts_targets_exact_row_ids():
	session = _BumpFakeSession()
	_bump_attempts(session, [1, 2, 3], _NOW)
	assert session.params["ids"] == [1, 2, 3]


class _CountFakeSession:
	def __init__(self, count):
		self._count = count
		self.sql = ""
		self.params = None

	def execute(self, stmt, params=None):
		self.sql = str(stmt)
		self.params = params
		return _ScalarOne(self._count)


class _ScalarOne:
	def __init__(self, value):
		self._value = value

	def scalar_one(self):
		return self._value


def test_count_pending_backlog_returns_the_count():
	session = _CountFakeSession(490)
	assert _count_pending_backlog(session, "acme_pm", None, max_attempts=3) == 490


def test_count_pending_backlog_omits_import_filter_when_none():
	session = _CountFakeSession(0)
	_count_pending_backlog(session, "acme_pm", None, max_attempts=3)
	assert "import_id" not in session.sql


def test_count_pending_backlog_includes_import_filter_when_given():
	session = _CountFakeSession(0)
	_count_pending_backlog(session, "acme_pm", "import-1", max_attempts=3)
	assert "import_id" in session.sql
	assert session.params["import_id"] == "import-1"
