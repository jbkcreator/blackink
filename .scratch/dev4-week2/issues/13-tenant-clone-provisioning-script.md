# Design the tenant clone / provisioning script

Type: grilling
Status: open
Blocked by: 03

## Question

Pilot Stage 1 runs `clone_tenant_environment.sh` to stand up an isolated tenant — this
script does not exist. Tenant/RLS primitives exist (`apply_clients.py`,
`config/tenant_policies.py`, `apply_rls_policies.py`); domains come from ticket 3.

Decide what provisioning entails and its shape: create the `clients` row (`founding = TRUE`),
county allocation, assign a 3-domain/6-mailbox cluster from the provisioned pool, set SMTP
credentials, verify zero credential leakage between tenant workspaces. Decide script vs
Python task-module, idempotency, and the exact ordered steps. Output: the provisioning
runbook/spec Stage 1 executes for clients #1 and #2.
