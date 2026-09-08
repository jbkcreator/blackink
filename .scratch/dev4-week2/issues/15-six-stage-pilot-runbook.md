# Assemble the six-stage founding-pilot runbook

Type: task
Status: open
Blocked by: 04, 07, 08, 10, 12, 13, 14

## Question

Task 4.3 is operational coordination: execute six stages for clients #1 and #2 — Tenant
Clone → Historical Ingest → Compliance Guard → Campaign Launch → Triage & Routing →
Booking & Dashboards — Sept 16–18, engineering hand-holding permitted.

HITL task: assemble the concrete runbook that stitches Dev 4's own pieces (sending verify 04,
ingest 07, SLA response 08, cadence 10, parsers 12, provisioning 13, wins dashboard 14) with
the **external givens** (Dev 1 settlement + `meeting_booked`, Dev 2 triage, Dev 3 Win-Back +
enrichment). Produce the ordered stage checklist, the go/no-go readiness gate per stage, the
live-monitoring plan, and a friction-log template to capture client-#1 issues before client
#2 starts.

**⚠ Depends on external givens absent from the repo as of 2026-09-07** (settlement, triage,
Win-Back, enrichment). The runbook must record their readiness as explicit pre-conditions,
not assume them silently. This is the map's integration endpoint.
