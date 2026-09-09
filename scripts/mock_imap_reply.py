"""Simulate an IMAP listener reply — publishes to ink:resume_signals.

Usage:
    python scripts/mock_imap_reply.py --work-order-id <id> [--latency-sec 7200]

If --work-order-id is omitted it prints the currently suspended work orders
from the Redis PEL so you can pick one.
"""
import argparse
import json
import sys

sys.path.insert(0, ".")

from src.core.redis_client import get_redis_client

RESUME_STREAM  = "ink:resume_signals"
RESUME_GROUP   = "ink_resume_workers"
WORK_STREAM    = "ink:work_orders"
WORK_GROUP     = "ink_workers"


def list_pending() -> None:
    r = get_redis_client()
    try:
        pending = r.xpending_range(WORK_STREAM, WORK_GROUP, min="-", max="+", count=20)
    except Exception as exc:
        print(f"Could not read PEL: {exc}")
        return
    if not pending:
        print("No pending work orders in PEL.")
        return
    print(f"{'MESSAGE ID':<30} {'WORK ORDER ID':<40} {'DELIVERIES'}")
    print("-" * 80)
    for entry in pending:
        mid = entry["message_id"]
        rows = r.xrange(WORK_STREAM, min=mid, max=mid)
        woid = rows[0][1].get("work_order_id", "?") if rows else "?"
        print(f"{mid:<30} {woid:<40} {entry.get('times_delivered', '?')}")


def publish_reply(work_order_id: str, latency_sec: int, loss_est: int) -> None:
    r = get_redis_client()
    try:
        r.xgroup_create(RESUME_STREAM, RESUME_GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise

    payload = json.dumps({"latency_sec": latency_sec, "loss_est": loss_est})
    msg_id = r.xadd(
        RESUME_STREAM,
        {"work_order_id": work_order_id, "resume_payload": payload},
    )
    print(f"Published resume signal → {RESUME_STREAM}")
    print(f"  message_id    : {msg_id}")
    print(f"  work_order_id : {work_order_id}")
    print(f"  latency_sec   : {latency_sec}")
    print(f"  loss_est      : ${loss_est:,}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Mock IMAP reply publisher")
    parser.add_argument("--work-order-id", default=None, help="Work order to resume")
    parser.add_argument("--latency-sec",   type=int, default=7200, help="Simulated reply latency (default 7200 = 2h)")
    parser.add_argument("--loss-est",      type=int, default=None, help="Override loss estimate (auto-computed if omitted)")
    args = parser.parse_args()

    if args.work_order_id is None:
        print("No --work-order-id given. Listing pending work orders:\n")
        list_pending()
        return

    import math
    loss_est = args.loss_est
    if loss_est is None:
        decay    = 1.0 - math.exp(-0.0005 * args.latency_sec)
        loss_est = int(8 * decay * 1_200 * 2.5)

    publish_reply(args.work_order_id, args.latency_sec, loss_est)


if __name__ == "__main__":
    main()
