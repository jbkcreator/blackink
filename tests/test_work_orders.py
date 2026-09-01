"""Unit tests for src.services.work_orders — no live DB.

FORK of FA/tests/test_relay_queue.py, adapted to agent_work_orders' richer
CRUD surface (FA's relay_approval_queue only needed enqueue/record_decision;
this table also needs update_payload's hash-rotation and queued_depth for
Dev 2's Cora throttle). The RLS/cross-tenant-isolation assertion is
deliberately NOT here — that needs a live Postgres and belongs in
tests/test_tenant_isolation.py, per the Dev 3 plan §3.7.

A minimal in-memory fake table stands in for agent_work_orders: this is
richer than test_compliance_gate.py's single-query FakeSession because
work_orders does full CRUD (INSERT / SELECT / two different UPDATE shapes /
COUNT), not one read.
"""

import json
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import IntegrityError

from src.services import work_orders as wo


class _FakeResult:
	def __init__(self, rows: list):
		self._rows = rows

	def mappings(self):
		return self

	def first(self):
		return self._rows[0] if self._rows else None

	def all(self):
		return list(self._rows)

	def scalar(self):
		return self._rows[0] if self._rows else None


_DEFAULTS = {
	"opportunity_id": None,
	"confidence_score": None,
	"slack_channel_id": None,
	"slack_message_ts": None,
	"decided_by": None,
	"decided_at": None,
	"execution_receipt": None,
	"error": None,
	"due_at": None,
}


class FakeTable:
	def __init__(self):
		self.rows: dict = {}


class FakeSession:
	def __init__(self, table: FakeTable):
		self.table = table

	def execute(self, stmt, params=None):
		"""Routes on statement kind (INSERT/SELECT/UPDATE) plus the SHAPE of
		params — not substring-matching the raw SQL text, which is fragile
		here on purpose: agent_work_orders has real columns literally named
		client_id and idempotency_key, so naive text search over a SELECT's
		own column list collides with search terms meant to match a WHERE
		clause. Routing on which keys the caller actually bound is
		unambiguous regardless of column names."""
		sql = " ".join(str(stmt).split())  # normalize whitespace
		params = params or {}
		keys = set(params.keys())

		if sql.startswith("INSERT INTO agent_work_orders"):
			return self._insert(params)

		if sql.startswith("SELECT COUNT"):
			return self._count(params)

		if sql.startswith("SELECT"):
			if keys == {"action_id", "client_id"}:
				row = self.table.rows.get(params["action_id"])
				matches = row is not None and row["client_id"] == params["client_id"]
				return _FakeResult([dict(row)] if matches else [])
			if keys == {"client_id", "idempotency_key"}:
				match = [
					dict(r)
					for r in self.table.rows.values()
					if r["client_id"] == params["client_id"] and r["idempotency_key"] == params["idempotency_key"]
				]
				return _FakeResult(match)
			if "limit" in keys and "action_id" not in keys and "idempotency_key" not in keys:
				return self._approved_batch(params)
			raise NotImplementedError(f"FakeSession: unrecognized SELECT param shape: {sorted(keys)}")

		if sql.startswith("UPDATE"):
			if {"channel_id", "message_ts"} <= keys:
				return self._update_slack_message(params)
			if {"payload", "config_fingerprint", "payload_hash"} <= keys:
				return self._update_payload(params)
			if {"new_status", "receipt", "error"} <= keys:
				return self._record_execution_result(params)
			if {"decision", "decided_by"} <= keys:
				return self._update_status(params, decision_field="decision")
			if {"until", "decided_by"} <= keys:
				return self._update_status(params, decision_field=None)
			if keys == {"action_id", "client_id"}:
				return self._claim_for_execution(params)
			raise NotImplementedError(f"FakeSession: unrecognized UPDATE param shape: {sorted(keys)}")

		raise NotImplementedError(f"FakeSession cannot handle: {sql[:80]}")

	def _insert(self, params):
		"""psycopg2 requires JSONB columns be bound as pre-serialized JSON
		strings (can't adapt a raw dict), confirmed against the real
		server — so work_orders.enqueue() now json.dumps() payload/
		config_fingerprint before binding. This fake models the DB's own
		round-trip: accepts the string on write, decodes back to a dict
		here (psycopg2 auto-deserializes JSONB on SELECT), so callers
		reading a WorkOrder back out see the same dict shape a real query
		would return."""
		for r in self.table.rows.values():
			if r["client_id"] == params["client_id"] and r["idempotency_key"] == params["idempotency_key"]:
				raise IntegrityError("duplicate idempotency_key", None, Exception("unique violation"))
		now = datetime.now(timezone.utc)
		row = {
			**_DEFAULTS,
			**params,
			"payload": json.loads(params["payload"]),
			"config_fingerprint": json.loads(params["config_fingerprint"]),
			"status": "QUEUED",
			"created_at": now,
			"updated_at": now,
		}
		self.table.rows[params["action_id"]] = row
		return SimpleNamespace(rowcount=1)

	def _update_slack_message(self, params):
		row = self.table.rows.get(params["action_id"])
		if row and row["client_id"] == params["client_id"]:
			row["slack_channel_id"] = params["channel_id"]
			row["slack_message_ts"] = params["message_ts"]
			return SimpleNamespace(rowcount=1)
		return SimpleNamespace(rowcount=0)

	def _update_status(self, params, *, decision_field):
		row = self.table.rows.get(params["action_id"])
		if row and row["client_id"] == params["client_id"] and row["status"] == "QUEUED":
			row["status"] = params[decision_field] if decision_field else "SNOOZED"
			row["decided_by"] = params["decided_by"]
			row["decided_at"] = datetime.now(timezone.utc)
			if "until" in params:
				row["due_at"] = params["until"]
			return SimpleNamespace(rowcount=1)
		return SimpleNamespace(rowcount=0)

	def _update_payload(self, params):
		row = self.table.rows.get(params["action_id"])
		if row and row["client_id"] == params["client_id"] and row["status"] == "QUEUED":
			row["payload"] = json.loads(params["payload"])
			row["config_fingerprint"] = json.loads(params["config_fingerprint"])
			row["payload_hash"] = params["payload_hash"]
			return SimpleNamespace(rowcount=1)
		return SimpleNamespace(rowcount=0)

	def _approved_batch(self, params):
		rows = [dict(r) for r in self.table.rows.values() if r["status"] == "APPROVED"]
		if "client_id" in params:
			rows = [r for r in rows if r["client_id"] == params["client_id"]]
		rows.sort(key=lambda r: r["created_at"])
		return _FakeResult(rows[: params["limit"]])

	def _claim_for_execution(self, params):
		row = self.table.rows.get(params["action_id"])
		if row and row["client_id"] == params["client_id"] and row["status"] == "APPROVED":
			row["status"] = "EXECUTING"
			return SimpleNamespace(rowcount=1)
		return SimpleNamespace(rowcount=0)

	def _record_execution_result(self, params):
		row = self.table.rows.get(params["action_id"])
		if row and row["client_id"] == params["client_id"] and row["status"] == "EXECUTING":
			row["status"] = params["new_status"]
			row["execution_receipt"] = json.loads(params["receipt"])
			row["error"] = params["error"]
			return SimpleNamespace(rowcount=1)
		return SimpleNamespace(rowcount=0)

	def _count(self, params):
		rows = list(self.table.rows.values())
		rows = [r for r in rows if r["status"] == "QUEUED"]
		if "client_id" in params:
			rows = [r for r in rows if r["client_id"] == params["client_id"]]
		if "cutoff" in params:
			rows = [r for r in rows if r["created_at"] <= params["cutoff"]]
		return _FakeResult([len(rows)])


@pytest.fixture
def fake_db(monkeypatch):
	table = FakeTable()

	@contextmanager
	def _fake_get_db_context(client_id=None):
		yield FakeSession(table)

	@contextmanager
	def _fake_get_system_db_context():
		yield FakeSession(table)

	monkeypatch.setattr(wo, "get_db_context", _fake_get_db_context)
	monkeypatch.setattr(wo, "get_system_db_context", _fake_get_system_db_context)
	return table


def _enqueue(client_id="acme_pm", idempotency_key=None, **overrides):
	kwargs = dict(
		client_id=client_id,
		entity_type="contact",
		entity_id="contact-1",
		agent_id="CAMPAIGN_AGENT",
		action_class="DISPATCH_EMAIL_TOUCH",
		autonomy_band="BAND_2_ONE_TAP",
		risk_class="LOW",
		payload={"subject": "Hi", "body": "Hello"},
		config_fingerprint={"channel": "email"},
		idempotency_key=idempotency_key or str(uuid.uuid4()),
	)
	kwargs.update(overrides)
	return wo.enqueue(**kwargs)


# ── enqueue idempotency ──────────────────────────────────────────────────


def test_enqueue_is_idempotent_on_client_and_key(fake_db):
	order1 = _enqueue(idempotency_key="fixed-key")
	order2 = _enqueue(idempotency_key="fixed-key")
	assert order1.action_id == order2.action_id
	assert len(fake_db.rows) == 1


def test_same_idempotency_key_different_clients_are_separate_rows(fake_db):
	a = _enqueue(client_id="client_a", idempotency_key="same-key")
	b = _enqueue(client_id="client_b", idempotency_key="same-key")
	assert a.action_id != b.action_id
	assert len(fake_db.rows) == 2


def test_enqueue_computes_action_id_before_insert(fake_db):
	order = _enqueue()
	# action_id is a real uuid4, not empty/None, and payload_hash is a real digest
	assert uuid.UUID(order.action_id)
	assert len(order.payload_hash) == 64


def test_enqueue_hash_matches_payload_hash_compute(fake_db):
	order = _enqueue()
	from src.services.slack import payload_hash

	assert order.payload_hash == payload_hash.compute(order)


# ── get() defense-in-depth (explicit client_id filter, not RLS alone) ───


def test_get_with_correct_client_id_returns_the_order(fake_db):
	order = _enqueue(client_id="acme_pm")
	assert wo.get("acme_pm", order.action_id) is not None


def test_get_with_wrong_client_id_returns_none_even_if_action_id_is_right(fake_db):
	"""SQL-level filter exercised here — the live-Postgres RLS backstop
	itself is a separate, out-of-scope test per the Dev 3 plan §3.7."""
	order = _enqueue(client_id="acme_pm")
	assert wo.get("some_other_client", order.action_id) is None


# ── record_decision ──────────────────────────────────────────────────────


def test_record_decision_on_queued_row_succeeds(fake_db):
	order = _enqueue()
	decided = wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	assert decided is not None
	assert decided.status == "APPROVED"
	assert decided.decided_by == "slack:U1"


def test_record_decision_on_non_queued_row_is_a_noop_double_tap_guard(fake_db):
	order = _enqueue()
	wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	second = wo.record_decision(order.client_id, order.action_id, decision="REJECTED", decided_by="slack:U2")
	assert second is None
	# The FIRST decision must not be overwritten by the second, no-op attempt.
	assert wo.get(order.client_id, order.action_id).status == "APPROVED"


def test_record_decision_with_mismatched_client_id_is_a_noop(fake_db):
	order = _enqueue(client_id="client_a")
	result = wo.record_decision("client_b", order.action_id, decision="APPROVED", decided_by="slack:U1")
	assert result is None
	assert wo.get("client_a", order.action_id).status == "QUEUED"


def test_record_decision_rejects_non_terminal_decision(fake_db):
	order = _enqueue()
	with pytest.raises(ValueError):
		wo.record_decision(order.client_id, order.action_id, decision="QUEUED", decided_by="slack:U1")


# ── snooze ────────────────────────────────────────────────────────────────


def test_snooze_sets_status_and_due_at(fake_db):
	order = _enqueue()
	until = datetime.now(timezone.utc) + timedelta(hours=4)
	snoozed = wo.snooze(order.client_id, order.action_id, until=until, decided_by="slack:U1")
	assert snoozed.status == "SNOOZED"
	assert snoozed.due_at == until


# ── update_payload / hash rotation ──────────────────────────────────────


def test_update_payload_recomputes_hash(fake_db):
	order = _enqueue()
	original_hash = order.payload_hash
	updated = wo.update_payload(
		order.client_id, order.action_id,
		payload={"subject": "Revised", "body": "New content"},
		config_fingerprint=order.config_fingerprint,
	)
	assert updated is not None
	assert updated.payload_hash != original_hash

	from src.services.slack import payload_hash

	assert updated.payload_hash == payload_hash.compute(updated)


def test_update_payload_refuses_a_decided_order(fake_db):
	order = _enqueue()
	wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	result = wo.update_payload(
		order.client_id, order.action_id, payload={"subject": "x", "body": "y"}, config_fingerprint={}
	)
	assert result is None


# ── queued_depth (what Dev 2's Cora throttle reads) ─────────────────────


def test_queued_depth_counts_only_queued_rows_for_one_client(fake_db):
	_enqueue(client_id="acme_pm")
	_enqueue(client_id="acme_pm")
	other = _enqueue(client_id="other_client")
	wo.record_decision(other.client_id, other.action_id, decision="APPROVED", decided_by="slack:U1")

	assert wo.queued_depth("acme_pm") == 2
	assert wo.queued_depth("other_client") == 0


def test_queued_depth_older_than_filters_by_age(fake_db):
	order = _enqueue()
	# created_at is "now" — nothing is older than a negative window.
	assert wo.queued_depth(order.client_id, older_than=timedelta(days=1)) == 0


# ── set_slack_message ────────────────────────────────────────────────────


def test_set_slack_message_stores_both_channel_and_ts(fake_db):
	order = _enqueue()
	wo.set_slack_message(order.client_id, order.action_id, channel_id="C123", message_ts="1234.5678")
	refreshed = wo.get(order.client_id, order.action_id)
	assert refreshed.slack_channel_id == "C123"
	assert refreshed.slack_message_ts == "1234.5678"


# ── default_idempotency_key ──────────────────────────────────────────────


def test_default_idempotency_key_is_deterministic_within_the_same_hour_bucket():
	now = datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)
	k1 = wo.default_idempotency_key("acme_pm", "contact-1", "DISPATCH_EMAIL_TOUCH", now)
	k2 = wo.default_idempotency_key("acme_pm", "contact-1", "DISPATCH_EMAIL_TOUCH", now.replace(minute=45))
	assert k1 == k2


def test_default_idempotency_key_differs_across_hour_buckets():
	now = datetime(2026, 9, 1, 14, 30, tzinfo=timezone.utc)
	later = datetime(2026, 9, 1, 15, 30, tzinfo=timezone.utc)
	k1 = wo.default_idempotency_key("acme_pm", "contact-1", "DISPATCH_EMAIL_TOUCH", now)
	k2 = wo.default_idempotency_key("acme_pm", "contact-1", "DISPATCH_EMAIL_TOUCH", later)
	assert k1 != k2


# ── approved_batch / claim_for_execution / record_execution_result ──────
# (--sweep's APPROVED -> EXECUTING -> DONE/FAILED state machine)


def test_approved_batch_returns_only_approved_rows_for_one_client(fake_db):
	order = _enqueue(client_id="acme_pm")
	wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	still_queued = _enqueue(client_id="acme_pm")
	other_client_approved = _enqueue(client_id="other_client")
	wo.record_decision(other_client_approved.client_id, other_client_approved.action_id, decision="APPROVED", decided_by="slack:U1")

	batch = wo.approved_batch("acme_pm")
	assert [o.action_id for o in batch] == [order.action_id]


def test_approved_batch_unscoped_aggregates_across_clients(fake_db):
	a = _enqueue(client_id="acme_pm")
	wo.record_decision(a.client_id, a.action_id, decision="APPROVED", decided_by="slack:U1")
	b = _enqueue(client_id="other_client")
	wo.record_decision(b.client_id, b.action_id, decision="APPROVED", decided_by="slack:U1")

	batch = wo.approved_batch(None)
	assert {o.action_id for o in batch} == {a.action_id, b.action_id}


def test_claim_for_execution_transitions_approved_to_executing(fake_db):
	order = _enqueue()
	wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	claimed = wo.claim_for_execution(order.client_id, order.action_id)
	assert claimed is not None
	assert claimed.status == "EXECUTING"


def test_claim_for_execution_refuses_a_non_approved_row(fake_db):
	order = _enqueue()  # still QUEUED, never approved
	claimed = wo.claim_for_execution(order.client_id, order.action_id)
	assert claimed is None


def test_claim_for_execution_second_claim_loses_the_race(fake_db):
	"""Two concurrent --sweep runs both try to claim the same APPROVED
	row — only the first succeeds, the second must not silently re-claim
	an already-EXECUTING row."""
	order = _enqueue()
	wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	first = wo.claim_for_execution(order.client_id, order.action_id)
	second = wo.claim_for_execution(order.client_id, order.action_id)
	assert first is not None
	assert second is None


def test_record_execution_result_success_sets_done(fake_db):
	order = _enqueue()
	wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	wo.claim_for_execution(order.client_id, order.action_id)
	result = wo.record_execution_result(order.client_id, order.action_id, success=True, receipt={"dispatcher": "noop"})
	assert result is not None
	assert result.status == "DONE"
	assert result.execution_receipt == {"dispatcher": "noop"}


def test_record_execution_result_failure_sets_failed_with_error(fake_db):
	order = _enqueue()
	wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	wo.claim_for_execution(order.client_id, order.action_id)
	result = wo.record_execution_result(order.client_id, order.action_id, success=False, receipt={}, error="dispatch failed")
	assert result.status == "FAILED"
	assert result.error == "dispatch failed"


def test_record_execution_result_refuses_a_row_not_claimed_first(fake_db):
	"""Cannot jump straight from APPROVED to DONE/FAILED — must go
	through claim_for_execution (EXECUTING) first."""
	order = _enqueue()
	wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	result = wo.record_execution_result(order.client_id, order.action_id, success=True, receipt={})
	assert result is None


def test_full_state_machine_queued_to_done(fake_db):
	"""QUEUED -> APPROVED -> EXECUTING -> DONE, end to end — the exact
	chain the Dev 3 plan's AC #1 demo needs to prove closes."""
	order = _enqueue()
	assert order.status == "QUEUED"
	approved = wo.record_decision(order.client_id, order.action_id, decision="APPROVED", decided_by="slack:U1")
	assert approved.status == "APPROVED"
	executing = wo.claim_for_execution(order.client_id, order.action_id)
	assert executing.status == "EXECUTING"
	done = wo.record_execution_result(order.client_id, order.action_id, success=True, receipt={"dispatcher": "noop"})
	assert done.status == "DONE"
