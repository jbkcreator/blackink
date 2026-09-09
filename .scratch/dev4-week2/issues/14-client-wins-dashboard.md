# Client Wins Dashboard: adapt sandbox view vs build new

Type: grilling
Status: open
Blocked by: —

## Question

Pilot Stage 6 DoD requires a per-client "Client Wins Dashboard" showing live client #1 data
(not empty panels, not sandbox data). Repo has only a **platform-wide** metrics digest
(`metrics_router.py`) and a **demo-sandbox** dashboard (`sandbox_router.py`,
`demo_sandbox_dashboard` view, `seed_demo_sandbox.py`) — no per-tenant client-facing wins view.

Decide: adapt the existing `demo_sandbox_dashboard` view/router to a per-`client_id` wins
view, or build a new one? What metrics/panels does "wins" show (bookings, replies,
sits, pipeline)? Client-facing surface (portal page vs Google Sheet like the sandbox)?
**Scope check**: confirm this is Dev 4's lane and not owned by whoever holds client-facing
dashboards — if not mine, rule out of scope. Output: the wins-dashboard spec or a scope call.
