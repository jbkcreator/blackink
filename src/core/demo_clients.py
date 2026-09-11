"""Central registry of demo / test tenant client_ids that must NEVER count
toward platform-wide production metrics.

Both the executive Slack digest (src/tasks/daily_digest.py) and the internal
metrics endpoint (src/api/metrics_router.py) aggregate across every tenant.
Any client_id seeded with synthetic activity for demos (the sales sandbox, the
Client Wins demo tenant) would otherwise inflate real reported pipeline numbers
whenever its events fall inside the reporting window (PR #53 review finding 1).

Defined once here so a new demo tenant is excluded from every aggregate by
adding a single entry, rather than drifting per-query literals.
"""

from __future__ import annotations

# Sales demo sandbox (blueprint §3.1.6) and the S-21 Client Wins demo tenant.
DEMO_CLIENT_IDS = (
    "DEMO_FRIDAY_SANDBOX",
    "DEMO_CLIENT_WINS",
)
