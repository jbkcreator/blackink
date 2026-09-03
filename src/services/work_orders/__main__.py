"""Work-order queue runner CLI — Dev 3 plan §3, item 1 ("queue runners").
FORK of FA/src/services/relay/__main__.py:225-277, adapted: --venture ->
--client-id (required everywhere, never defaulted — an unscoped --sweep
would silently return zero rows under RLS, which looks like "nothing to
do" rather than a misconfiguration, so this refuses to run without it).

    python -m src.services.work_orders --health  --client-id _platform_internal
    python -m src.services.work_orders --seed    --client-id _platform_internal \\
            --action-class DISPATCH_EMAIL_TOUCH --entity-type contact --entity-id demo-1 \\
            --recipient test@example.com --payload-json '{"subject":"Hi","body":"Hello"}' \\
            --channel-key setter
    python -m src.services.work_orders --sweep   --client-id _platform_internal

--seed is the AC #1 demo driver: Week 0 has no agent producing work orders
yet, so this is how a card actually gets posted for a human to click.
--sweep is the AC #1 state-machine proof: it drives an APPROVED row through
EXECUTING to DONE against the `noop` dispatcher (src.services.work_orders.
dispatchers) — Week 1's Campaign Agent registers the real email dispatcher
in that same table, this file does not change when it does.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

sys.path.insert(0, ".")

from src.agents.relay import halt_service
from src.services import work_orders as wo
from src.services.work_orders.dispatchers import DISPATCHERS


def _line(msg: str) -> None:
	print(msg)


def cmd_health(client_id: str) -> int:
	from config.settings import get_settings
	from config.slack_channels import CHANNEL_REGISTRY, resolve_channel_id

	settings = get_settings()
	problems = []

	try:
		depth = wo.queued_depth(client_id)
		_line(f"DB reachable — queued_depth({client_id!r}) = {depth}")
	except Exception as exc:
		problems.append(f"DB: {exc}")

	if not settings.slack_bot_token:
		problems.append("SLACK_BOT_TOKEN not set")
	if not settings.slack_signing_secret:
		problems.append("SLACK_SIGNING_SECRET not set")
	if not settings.slack_app_token:
		problems.append("SLACK_APP_TOKEN not set (needed for Socket Mode)")
	if not settings.relay_resume_secret:
		problems.append("RELAY_RESUME_SECRET not set")
	if not settings.blackink_global_approvers:
		problems.append("BLACKINK_GLOBAL_APPROVERS is empty — every click will be rejected as unauthorized")

	for key in CHANNEL_REGISTRY:
		if not resolve_channel_id(key):
			problems.append(f"channel {key!r} has no configured ID")

	try:
		halted = halt_service.is_halted(client_id=client_id)
		_line(f"halt check reachable — is_halted({client_id!r}) = {halted}")
	except Exception as exc:
		problems.append(f"halt_service: {exc}")

	if problems:
		_line("health: PROBLEMS FOUND")
		for p in problems:
			_line(f"  - {p}")
		return 1
	_line("health: all checks passed")
	return 0


def cmd_seed(args: argparse.Namespace) -> int:
	try:
		payload = json.loads(args.payload_json) if args.payload_json else {}
	except json.JSONDecodeError as exc:
		_line(f"--payload-json is not valid JSON: {exc}")
		return 1

	idempotency_key = args.idempotency_key or wo.default_idempotency_key(
		args.client_id, args.entity_id, args.action_class, datetime.now(timezone.utc)
	)

	order = wo.enqueue(
		client_id=args.client_id,
		entity_type=args.entity_type,
		entity_id=args.entity_id,
		agent_id=args.agent_id,
		action_class=args.action_class,
		autonomy_band=args.autonomy_band,
		risk_class=args.risk_class,
		payload=payload,
		config_fingerprint={"channel": args.channel_key, "seeded_via": "cli"},
		idempotency_key=idempotency_key,
		recipient=args.recipient,
	)
	_line(f"enqueued action_id={order.action_id} status={order.status}")

	from src.services.slack.listeners import post_work_order_card

	posted = asyncio.run(post_work_order_card(order, channel_key=args.channel_key))
	if posted is None:
		_line("card NOT posted (Slack unconfigured, channel unresolved, or API error — see logs)")
		return 0
	refreshed = wo.get(order.client_id, order.action_id)
	if refreshed is None:
		_line("card posted, but the order vanished before it could be re-read — investigate")
		return 1
	_line(f"card posted — channel_id={refreshed.slack_channel_id} message_ts={refreshed.slack_message_ts}")
	return 0


def _recover_stalled(client_id: str) -> None:
	"""Two recovery steps that must run BEFORE approved_batch(), because
	both produce rows that approved_batch would otherwise never see:

	  1. Expired snoozes (SNOOZED + due_at passed) -> QUEUED, card reposted.
	     Without this a snooze never ends; see wo.requeue_due_snoozed.
	  2. Stale claims (EXECUTING, untouched past the timeout) -> APPROVED,
	     recovering work stranded by a worker that died mid-dispatch; see
	     wo.reclaim_stale_executing.

	Both are safe to run on every sweep: each is a single guarded UPDATE
	that matches nothing when there is nothing to recover."""
	revived = wo.requeue_due_snoozed(client_id)
	for order in revived:
		# "setter" to match _load_and_verify's own refreshed-card repost —
		# config_fingerprint["channel"] is the DISPATCHER key (email/noop),
		# not a Slack channel key, so it must not be used here.
		posted = _post_card(order)
		_line(
			f"snooze expired — action_id={order.action_id} back to QUEUED"
			+ ("" if posted else " (card NOT reposted — see logs)")
		)

	for action_id in wo.reclaim_stale_executing(client_id):
		_line(f"reclaimed stale EXECUTING action_id={action_id} -> APPROVED (worker likely died mid-dispatch)")


def _post_card(order) -> bool:
	"""Sync seam around the async post, matching cmd_seed's own
	asyncio.run() style — keeps _recover_stalled a plain sync function and
	gives the tests one thing to stub."""
	from src.services.slack.listeners import post_work_order_card

	return asyncio.run(post_work_order_card(order, channel_key="setter")) is not None


def cmd_sweep(client_id: str) -> int:
	if halt_service.is_halted(client_id=client_id):
		_line(f"HALTED — client_id={client_id!r} is halted (global or client-scoped); executing zero items")
		return 0

	_recover_stalled(client_id)

	batch = wo.approved_batch(client_id)
	if not batch:
		_line("sweep: no approved items")
		return 0

	sent = failed = deferred = 0
	for order in batch:
		if halt_service.is_halted(client_id=order.client_id):
			_line(f"HALTED mid-sweep — {len(batch) - sent - failed - deferred} item(s) left unclaimed for the next sweep")
			break

		claimed = wo.claim_for_execution(order.client_id, order.action_id)
		if claimed is None:
			_line(f"action_id={order.action_id} already claimed/moved — leaving untouched (idempotency)")
			deferred += 1
			continue

		dispatcher = DISPATCHERS.get(claimed.config_fingerprint.get("channel", "noop"), DISPATCHERS.get("noop"))
		try:
			receipt = dispatcher(claimed)
		except Exception as exc:
			finalised = wo.record_execution_result(claimed.client_id, claimed.action_id, success=False, receipt={}, error=str(exc))
			if finalised is None:
				# None means row was reclaimed mid-flight — alert #blackink-qa
				_line(f"action_id={claimed.action_id} RECLAIMED MID-FLIGHT during failure — result unrecordable; alert #blackink-qa")
			else:
				_line(f"action_id={claimed.action_id} dispatch FAILED: {exc}")
			failed += 1
			continue

		if receipt.get("defer"):
			from datetime import timedelta
			until = datetime.now(timezone.utc) + timedelta(hours=1)
			deferred_row = wo.defer_execution(claimed.client_id, claimed.action_id, until=until)
			if deferred_row is None:
				_line(f"action_id={claimed.action_id} RECLAIMED MID-FLIGHT during defer — alert #blackink-qa")
				failed += 1
			else:
				_line(f"action_id={claimed.action_id} DEFERRED (mailboxes at 24h cap) -> SNOOZED until {until.isoformat()}")
				deferred += 1
			continue

		finalised = wo.record_execution_result(claimed.client_id, claimed.action_id, success=True, receipt=receipt)
		if finalised is None:
			# None means row was reclaimed mid-flight — alert #blackink-qa
			_line(f"action_id={claimed.action_id} RECLAIMED MID-FLIGHT — dispatch ran but result unrecordable; alert #blackink-qa")
			failed += 1
			continue
		_line(f"action_id={claimed.action_id} DONE")
		sent += 1

	_line(f"sweep: {sent} sent, {failed} failed, {deferred} deferred")
	return 0


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description="agent_work_orders queue runner")
	parser.add_argument("--health", action="store_true", help="Run scaffolding health check and exit")
	parser.add_argument("--seed", action="store_true", help="Enqueue one work order and post its card")
	parser.add_argument("--sweep", action="store_true", help="Claim APPROVED items and dispatch them")
	parser.add_argument("--client-id", required=True, help="Required for every mode — no default")
	parser.add_argument("--action-class", default="DISPATCH_EMAIL_TOUCH")
	parser.add_argument("--entity-type", default="contact")
	parser.add_argument("--entity-id")
	parser.add_argument("--agent-id", default="CLI_SEED")
	parser.add_argument("--autonomy-band", default="BAND_2_ONE_TAP")
	parser.add_argument("--risk-class", default="LOW")
	parser.add_argument("--recipient")
	parser.add_argument("--payload-json", default="{}")
	parser.add_argument("--channel-key", default="setter")
	parser.add_argument("--idempotency-key")
	args = parser.parse_args(argv)

	if args.health:
		return cmd_health(args.client_id)
	if args.seed:
		if not args.entity_id:
			parser.error("--seed requires --entity-id")
		return cmd_seed(args)
	if args.sweep:
		return cmd_sweep(args.client_id)

	parser.error("one of --health, --seed, --sweep is required")
	return 2  # unreachable — parser.error exits


if __name__ == "__main__":
	raise SystemExit(main())
