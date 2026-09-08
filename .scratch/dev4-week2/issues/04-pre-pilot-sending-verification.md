# Pre-pilot sending verification

Type: task
Status: open
Blocked by: 01, 02, 03

## Question

Before the pilot arms, verify the tenant-isolated sending stack end-to-end against the
Task 4.1 Definition of Done: SPF/DKIM/DMARC green on all provisioned domains; single-mailbox
daily ceiling defers the 51st send; Tenant A dispatches only from Tenant A's cluster; a
simulated 4% bounce fires `domain_quarantined` and swaps a same-cluster reserve within
5 min; a cross-tenant dispatch attempt is blocked with `cross_tenant_blocked` logged.

Task: run these checks (reuse `tests/test_tenant_isolation.py` + a live local Postgres per
CLAUDE.md), record pass/fail per DoD line, and post the result summary to `#blackink-qa`.
Answer records any residual gaps blocking pilot.
