# Sending domain inventory

Supplied by the client, 2026-09-03. Answers [CLIENT-ASKS](CLIENT-ASKS.md) A2
(partially — see the shortfall below).

Maps onto the existing `sending_domains` table
(`migrations/apply_sending_domains.py`): `domain`, `client_id`,
`cluster_label`, `spf/dkim/dmarc_validated`, `warmup_status`, `health_score`,
`quarantine_state`, `is_reserve`.

**None of these are in the database yet**, and DNS/SPF/DKIM/DMARC setup plus
mailbox warmup is a **manual runbook, not code** (`CLAUDE.md`, matching Forced
Action's ADR 0011). Registering the rows is not the same as being able to send
from them.

---

## Pool A — Blackink's own outbound (5 domains)

| Domain | `is_reserve` |
|---|---|
| blackinkgrowth.com | false |
| hiblackink.com | false |
| blackinkdoors.com | false |
| tryblackink.com | false |
| blackinkleads.com | false |

Matches §7.3's "Blackink internal outbound: 5 domains / 10 mailboxes"
(2 mailboxes per domain).

Note: §7.3 lists *example* internal domains (`growth-blackink.com`,
`connect-blackink.com`, `audit-blackink.com`, `pm-blackink.com`,
`scale-blackink.com`) that do **not** match these. The blueprint's examples
were illustrative; **this list is authoritative**. The `sending_domain` value
in `outbound_touch_dispatched` payloads will therefore never be the string
shown in the blueprint's §8.1 example — do not treat that example as a
fixture.

## Pool B — client pools 1 and 2 (6 domains)

| Domain |
|---|
| ownersignalhq.com |
| ownerforgeco.com |
| landlordpathpro.com |
| landlordgridco.com |
| rentalflowgroup.com |
| realtybridgehq.com |

6 domains ÷ 3 per client = **2 client clusters**. Consistent with "pools 1
and 2".

## Pool C — client pools 3 to 5 (6 domains)

| Domain |
|---|
| investorwavehq.com |
| investorbasegroup.com |
| portfolioedgeco.com |
| portfolioloophub.com |
| realtysparkpro.com |
| assetdirecthub.com |

**6 domains, but three clients named.** At §7.3's 3-domain cluster, six
domains serve two clients, not three. See the shortfall below.

## Reserve — warmed, never sent from (3 domains)

| Domain | `is_reserve` |
|---|---|
| equitypulsepartners.com | true |
| capitalreachhq.com | true |
| trusteecompass.com | true |

These back §7.4's deliverability sentinel: on a bounce >3% or complaint
>0.08% trip within a rolling 48h, the degraded domain is quarantined and the
system "rotates to a pre-warmed reserve domain".

---

## ⚠ Shortfall against §7.3

§7.3 specifies **20 domains / 40 mailboxes**, split 5 internal (10 mailboxes)
and **15 client (30 mailboxes)**, with each active client holding a dedicated
**3-domain / 6-mailbox** cluster — i.e. 15 client domains serve 5 clients.

What we actually have:

| | Blueprint | Supplied | Gap |
|---|---:|---:|---:|
| Internal (Pool A) | 5 | 5 | ✅ |
| Client (Pools B + C) | 15 | 12 | **−3** |
| Reserve | not counted in the 20 | 3 | — |
| **Total** | 20 sendable + reserve | 20 incl. reserve | **−3 sendable** |

Two consequences:

1. **Pool C cannot serve three clients.** Six domains is two clusters.
   Client 5 has no domains.
2. **Mailbox count follows.** At 2 mailboxes per domain, 12 client domains
   yield **24 mailboxes, not 30** — so the fifth client cannot get its
   6-mailbox cluster, and §7.1's "the contact's client uses a 6-mailbox
   cluster" fails for them.

The reserve does not close the gap: reserve domains are explicitly never sent
from, and consuming them would leave the sentinel with nothing to fail over
to.

### Open questions

- Is the 5-client target still current, or has it been revised down to 4?
  At 4 clients, 12 domains is exactly right and there is no gap.
- If 5 clients: register 3 more domains **now**. Warmup is measured in weeks,
  so this is on the critical path well before any code needs them.
- Is 2 mailboxes per domain confirmed? The blueprint implies it
  (40 ÷ 20, 6 mailboxes ÷ 3 domains) but never states it. It drives how many
  mailboxes the LRU rotation in `get_active_mailbox_for_client` has to work
  with, and therefore how the 30–50/day cap interacts with volume
  (see ticket [08](issues/08-24h-send-cap-counting-substrate.md)).
- Which `client_id` owns Pool B cluster 1 vs cluster 2, and the same for
  Pool C? The pools are named but not yet assigned to actual clients, and
  `sending_domains.client_id` is NOT NULL-bearing for tenant isolation.
- Are the 3 reserve domains reserved **globally**, or one per pool? §7.4 says
  the sentinel promotes "a same-cluster reserve domain" — but a reserve
  domain assigned to no client is not in any cluster. This matters: if a
  Pool B domain is quarantined, can it fail over to a reserve that has never
  belonged to that client, given §7.3 forbids pooling sending identity across
  clients?

That last one is a genuine design conflict between §7.3 (no cross-client
identity pooling) and §7.4 (promote a reserve on quarantine), and it is worth
resolving before the sentinel ever fires in production.
