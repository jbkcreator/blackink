"""The sweep runner's recovery step — PR #4 review findings 3 and 4.

tests/test_work_orders.py proves the two recovery QUERIES in isolation.
This file proves they are actually WIRED INTO the sweep, which is the part
that makes a snoozed or crashed order reachable again in production: both
findings were "the query would fix it, but nothing calls it".

No live DB and no live Slack — the work_orders seam is stubbed, since what
is under test here is cmd_sweep's ORDERING (recover before approved_batch,
so recovered rows are picked up by the same run), not any SQL.
"""

import pytest

from src.services.work_orders import __main__ as runner


@pytest.fixture
def stub_sweep(monkeypatch):
	"""Records the call order and lets each test decide what recovery
	returns. asyncio.run() inside _recover_stalled is avoided entirely by
	stubbing the card repost."""
	calls: list = []

	monkeypatch.setattr(runner.halt_service, "is_halted", lambda client_id=None: False)
	monkeypatch.setattr(runner.wo, "requeue_due_snoozed", lambda cid: (calls.append("requeue"), [])[1])
	monkeypatch.setattr(runner.wo, "reclaim_stale_executing", lambda cid: (calls.append("reclaim"), [])[1])
	monkeypatch.setattr(runner.wo, "approved_batch", lambda cid: (calls.append("approved_batch"), [])[1])
	monkeypatch.setattr(runner, "_post_card", lambda order: True)
	return calls


def test_sweep_recovers_before_selecting_the_batch(stub_sweep):
	"""Ordering is the whole point: a snooze that expired and a claim that
	crashed must both be actionable on THIS sweep, not the next one."""
	assert runner.cmd_sweep("acme_pm") == 0
	assert stub_sweep == ["requeue", "reclaim", "approved_batch"]


def test_sweep_skips_recovery_entirely_when_halted(monkeypatch, stub_sweep):
	"""A halt must stop everything — reviving a snoozed order would repost
	a live, clickable card into Slack while the tenant is halted."""
	monkeypatch.setattr(runner.halt_service, "is_halted", lambda client_id=None: True)
	assert runner.cmd_sweep("acme_pm") == 0
	assert stub_sweep == []


def test_expired_snooze_reposts_its_card(monkeypatch, stub_sweep):
	from types import SimpleNamespace

	order = SimpleNamespace(action_id="abc123", client_id="acme_pm", config_fingerprint={"channel": "email"})
	monkeypatch.setattr(runner.wo, "requeue_due_snoozed", lambda cid: [order])
	reposted = []
	monkeypatch.setattr(runner, "_post_card", lambda o: (reposted.append(o), True)[1])

	assert runner.cmd_sweep("acme_pm") == 0
	assert reposted == [order]


def test_reclaimed_orders_are_dispatched_on_the_same_sweep(monkeypatch):
	"""End to end through cmd_sweep: a row stranded in EXECUTING is
	reclaimed to APPROVED and then actually dispatched, rather than waiting
	for a later run."""
	from types import SimpleNamespace

	order = SimpleNamespace(
		action_id="abc123",
		client_id="acme_pm",
		action_class="DISPATCH_EMAIL_TOUCH",  # read by the noop dispatcher's log line
		config_fingerprint={"channel": "noop"},
	)
	state = {"reclaimed": False}

	def _reclaim(cid):
		state["reclaimed"] = True
		return [order.action_id]

	monkeypatch.setattr(runner.halt_service, "is_halted", lambda client_id=None: False)
	monkeypatch.setattr(runner.wo, "requeue_due_snoozed", lambda cid: [])
	monkeypatch.setattr(runner.wo, "reclaim_stale_executing", _reclaim)
	# approved_batch only sees the row BECAUSE reclaim already ran.
	monkeypatch.setattr(runner.wo, "approved_batch", lambda cid: [order] if state["reclaimed"] else [])
	monkeypatch.setattr(runner.wo, "claim_for_execution", lambda cid, aid: order)

	results = []
	monkeypatch.setattr(
		runner.wo,
		"record_execution_result",
		lambda cid, aid, *, success, receipt, error=None: results.append((aid, success)),
	)

	assert runner.cmd_sweep("acme_pm") == 0
	assert results == [(order.action_id, True)]
