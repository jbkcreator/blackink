# Add 24-hour card expiry to the Slack payload-hash layer

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## ⚠ Correction (2026-09-03, later) — expiry IS required

`Week1_Tasks_Dev_Split_v2.md` shared Definition of Done, item 5:

> "Execute Approve, Revise, and Snooze actions in `#blackink-setter` and
> `#sales-replies`; verify payload-bound SHA-256 hash security blocks altered
> payloads; **verify expired-card rejection**."

So the conclusion below — that the ground truth omits expiry — was **too
narrow a reading**. Expiry is on the September 11 gate.

**Everything else in this resolution stands, and matters more now:** implement
expiry via **`slack_message_ts`**, not by changing the hash preimage. No
`HASH_VERSION` bump, no invalidation of other devs' live cards. The analysis
of *why* the preimage approach is wrong is unaffected.

`Mark Opt-Out` must still not expire.

## Resolution (2026-09-03)

### The ground truth does not require card expiry *(superseded — see above)*

Source of Truth §2.2:

> "Slack action cards bind the exact payload, recipient, and configuration to
> SHA-256. **Reject stale approvals after any change.**"

Blueprint p10:

> "If a draft payload or template is **modified in the background while
> awaiting review**, clicking an outdated Slack button is rejected by the
> backend."

Both define *stale* as **content drift, not age**. There is no 24-hour expiry
and no `timestamp_window` anywhere in the Source of Truth. Those appear only
in the downstream Dev 3 task reference §19.

**So `src/services/slack/payload_hash.py` already satisfies the client
requirement in full.** `HashVerdict.fresh` is exactly "the content changed
since this button was minted", and `integrity_ok` independently catches
stored-column drift. Both are unconditionally evaluated so a double failure
is never misreported as an ordinary stale click.

### Where expiry is still wanted, do it without touching the hash

The task DoD asks for it, and it is cheap — **but not via the preimage.**

Use **`agent_work_orders.slack_message_ts`**, already stored, already set when
the card is posted. Age is `now − slack_message_ts`, checked at click time
against a row `_load_and_verify` re-fetches anyway.

- **No `HASH_VERSION` bump**, so no invalidation of other devs' live cards and
  no cross-dev coordination — which was this ticket's main scope worry.
- **No preimage change**, so the FIXED-preimage rule in the module docstring
  is respected.
- **Not forgeable**: the timestamp never travels in the button, so there is
  nothing for an attacker to tamper with. Binding time into the hash would add
  no security here.

**Rejected alternatives:**

- *Bucketed timestamp in the preimage* (the task doc's literal formula) — a
  card minted at 23:59 of a bucket expires in one minute, and verification
  must try multiple buckets. Surprising and fiddly.
- *`created_at`* — wrong field. Ticket 06's `due_at` gating means a card can
  be enqueued days before it is posted; `created_at` would expire cards before
  anyone ever saw them.

### `Mark Opt-Out` must not expire

Expiry is uniform across action classes **except** opt-out. Refusing to record
an opt-out because its card is 25 hours old is the worse failure in every
direction: an opt-out honoured late is still honoured, while an opt-out
*dropped* is a compliance breach and an angry prospect.

Recommended: opt-out actions bypass the age check entirely. If that is
uncomfortable, the fallback is to record the opt-out while refusing the card's
other side effects — but simply rejecting the click is not acceptable.

### Distinguish expired from altered in logs

The user-facing string stays as specified — "This action has expired or was
altered" — deliberately vague, since telling a user *which* is more
information than they need. But the log lines must separate them. The
listener already logs `work_order_hash_integrity_failure` and
`work_order_stale_click_rejected` as distinct events; add a third for expiry
rather than folding it into either.

## Question

The shared Slack security spec (§19) requires:

```
card_hash = SHA-256(payload + recipient_id + config_state + timestamp_window)
```

with cards expiring after 24 hours, expired interactions rejected, and the
message `This action has expired or was altered`. 3.1.3's DoD requires this.

What exists in `src/services/slack/payload_hash.py` is a real SHA-256
binding over `{v, client_id, action_id, action_class, entity_type,
entity_id, recipient, payload, config}` with `hmac.compare_digest` and
fail-closed behaviour on non-string input — good, but it has **no
`timestamp_window` component and no expiry**. `HashVerdict` exposes `fresh`,
but freshness there means content-drift, not age.

Decide:

- **What is a `timestamp_window`?** A rounded epoch bucket (and if so, what
  bucket size, and how are boundary straddles handled — a card issued at
  23:59 into a 24h bucket effectively expires in a minute), or an explicit
  `issued_at` carried in the button value and range-checked on receipt?
  The bucket approach binds time into the hash as the spec's formula
  literally reads; the `issued_at` approach is far less surprising.
- Changing the preimage is a **`HASH_VERSION` bump**. What happens to cards
  already posted under the old version — rejected, or grandfathered? Since
  the whole point is rejecting stale cards, rejection is defensible, but say
  so deliberately.
- Is 24h **uniform across action classes**? A `Mark Opt-Out` card arguably
  should never expire — an opt-out honoured late is still an opt-out
  honoured, and rejecting it is the worse failure. Compare against a
  `Book Meeting` card, where stale state genuinely matters.
- Does the **UX string** exactly match §19, and is it distinguishable in
  logs between *expired* and *altered*? Merging them is friendlier to users
  and worse for debugging.

## Scope note

Touches shared Slack infrastructure used by other devs' cards, not just
Task 3.1's. Coordinate before bumping the version.
