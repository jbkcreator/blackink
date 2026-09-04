# Make `sending_domain` reachable from mailbox assignment

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03) — this turned out to be two live bugs

### 🐞 Bug 1: domain quarantine does not stop dispatch

`src/tasks/deliverability_sentinel.py:81` updates **only**
`sending_domains.quarantine_state`. It never touches
`mailboxes.quarantine_state`. `get_active_mailbox_for_client` filters on
`mailboxes.quarantine_state = 'active'` and **never joins `sending_domains`**.

So when the sentinel trips — bounce >3% or complaints >0.08% rolling 48h — it
marks the domain quarantined, promotes a reserve, alerts `#blackink-qa`, and
**the dispatcher keeps handing out mailboxes on the quarantined domain.**

**This is an unmet requirement, not a design preference.** Blueprint p16:

> "the Sentinel trips an automatic quarantine: **pauses outbound dispatch**,
> replaces the degraded domain with a pre-warmed reserve domain, and posts an
> alert"

It does the replace and the alert. It does not pause dispatch. §2.2's
"governed warmed-reserve handling" is not governing anything.

Harmless today only because nothing sends. Becomes live the moment ticket 05's
dispatcher registers — and the failure mode is *continuing to send from a
domain we already know is burning*, which is how the other domains in the
cluster follow it down.

**Fix: the dispatcher joins and checks domain state.** Not a sentinel cascade.

```sql
FROM mailboxes m JOIN sending_domains d ON d.id = m.domain_id
WHERE m.client_id = :client_id
  AND m.warmup_status = 'warmed'  AND m.quarantine_state = 'active'
  AND d.warmup_status = 'warmed'  AND d.quarantine_state = 'active'
```

Single source of truth. A cascade denormalizes state into two places that can
drift, and anything quarantining a domain *outside* the sentinel — a manual
DB fix during an incident, most likely — would silently fail to protect.

### 🐞 Bug 2: the sentinel can find no reserve to promote

Same file, line 92:

```sql
SELECT id FROM sending_domains
WHERE cluster_label = :cluster AND is_reserve = TRUE AND quarantine_state = 'reserve'
```

It requires a reserve **in the same cluster**. The three supplied reserve
domains (`equitypulsepartners.com`, `capitalreachhq.com`,
`trusteecompass.com` — see `SENDING-DOMAINS.md`) are a **global** pool
belonging to no cluster. On a real trip the query returns nothing and
**nothing is promoted**.

`SENDING-DOMAINS.md` raised this as a design question — whether reserves are
global or per-cluster, given §7.3 forbids pooling sending identity across
clients while §7.4 wants same-cluster promotion. It is not merely a question:
as configured, the promotion path is dead. Either assign reserves to clusters
at provisioning time, or relax the query and accept a cross-client reserve —
which needs an explicit ruling against §7.3.

### The original question: `sending_domain` reachability

**Answered by the same join.** `mailboxes.domain_id` is a `NOT NULL` FK to
`sending_domains(id)`, so the join yields `d.domain` — the mandatory
`outbound_touch_dispatched` payload key — in the same round trip as the
quarantine check.

**Do not derive it** by splitting `mailbox_address` on `@`. The join is
required anyway for Bug 1, so derivation buys nothing and risks silent
divergence if a mailbox address and its domain row ever disagree.

**Extend `MailboxAssignment`** with `sending_domain` (and `domain_id`,
`cluster_label` — both free once joined, and the sentinel work will want
them). Check callers before changing the shared return type.

### Cluster-aware rotation: not needed

§7.2 / blueprint p16 asks for rotation "evenly across the client's **6
assigned mailboxes**" — mailbox-level, which LRU already satisfies. At 2
mailboxes per domain, even mailbox spread yields even domain spread for free.
No change.

## Question

`sending_domain` is one of the six **mandatory** payload keys on
`outbound_touch_dispatched` (§8, §27.2) — omit it and the logger raises
`MalformedEventError` and drops the event.

But `get_active_mailbox_for_client` returns
`MailboxAssignment(mailbox_id, mailbox_address, instantly_account_email,
client_id)`. It never joins `sending_domains`, so the caller cannot supply
the key. The dispatcher is mailbox-level only and is not domain- or
cluster-aware.

Decide:

- **Extend `MailboxAssignment`** with `sending_domain` (and possibly
  `domain_id`, `cluster_label`) by joining `mailboxes → sending_domains` in
  the existing query — one round trip, changes a shared return type — or
  **a second lookup** by the caller, which keeps the dispatcher untouched
  but invites every caller to forget it.
- Can `sending_domain` be **derived** from `mailbox_address` by splitting on
  `@`? Probably, but confirm that `mailboxes.mailbox_address`'s domain is
  always identical to its `sending_domains.domain` row. If it is, derivation
  is tempting and fragile; if it is not, derivation is a latent bug.
- Should selection become **cluster-aware**? §7.3 specifies a dedicated
  3-domain / 6-mailbox cluster per client, and §7.4's deliverability
  sentinel quarantines *domains* and promotes reserve domains. Pure mailbox
  LRU can concentrate sends on one domain. Does rotation need to spread
  across domains too, and must it exclude mailboxes whose domain is
  `quarantine_state`-flagged or not `warmup_status`-complete?

That last point is the substantive one: the sentinel already quarantines
domains, but if the dispatcher does not read `quarantine_state`, it will
keep handing out mailboxes on a quarantined domain. Verify current behaviour
before deciding — this may be a live bug rather than a design question.

## Scope note

Small and well-bounded, but touches a shared return type used elsewhere.
Check callers before changing the signature.
