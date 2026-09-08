# Wire per-client daily send ceiling

Type: grilling
Status: resolved
Blocked by: —

## Question

`mailbox_dispatcher.py` enforces a hardcoded `DEFAULT_DAILY_SEND_CAP = 50`; the
`clients.daily_send_ceiling` column exists but is not read by the picker. Task 4.1
requires 30–50/day/mailbox with rotation across the 6-mailbox cluster.

Decide: should the picker read `clients.daily_send_ceiling` as the per-client source of
truth (fallback 50 when NULL), or stay hardcoded? Is the ceiling per-mailbox or
per-cluster? What's the intended default for a founding client? Lock the source-of-truth
and the fallback behaviour so implementation is unambiguous.

## Answer

**Source of truth = `clients.daily_send_ceiling` (per mailbox).** `IMPLEMENTED` on branch
`feature/week2-4.1-tenant-sending`.

- `get_active_mailbox_for_client` signature changed to `daily_send_cap: Optional[int] = None`.
  When the caller passes nothing, the cap is resolved from `clients.daily_send_ceiling` via
  new helper `_resolve_daily_send_cap`.
- `daily_send_ceiling` defaults to `0`; **`0`/NULL is treated as "unset" → fall back to
  `DEFAULT_DAILY_SEND_CAP` (50)**, so a freshly-provisioned client is never floored to zero
  sends. A positive ceiling overrides.
- An explicit `daily_send_cap` argument still wins (tests / callers that know the cap) and
  skips the clients lookup.
- Cap is **per-mailbox** — the rolling-24h count is already keyed on `d.mailbox_id = m.id`.
- Tests: `tests/test_mailbox_dispatcher.py` — helpers prepend the ceiling row; added
  `test_cap_resolved_from_client_ceiling_when_caller_passes_none`,
  `test_zero_ceiling_falls_back_to_default_cap`, `test_explicit_cap_argument_skips_client_lookup`.
  38 passed.
