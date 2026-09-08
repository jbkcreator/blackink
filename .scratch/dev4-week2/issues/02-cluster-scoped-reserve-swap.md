# Cluster-scoped reserve domain swap

Type: grilling
Status: reopened
Blocked by: —

## Question

`deliverability_sentinel.py` quarantines a degraded domain and promotes a reserve, but the
promotion picks "any available reserve" (`cluster_label IS NULL`) — the module docstring
claims same-cluster promotion. Task 4.1 requires client A's degraded domain to be replaced
inside client A's own 3-domain cluster, never with a reserve from another tenant/cluster.

Decide: is the reserve pool per-cluster (each client cluster carries its own warm reserves)
or a shared warm pool? Reconcile code vs docstring. If per-cluster, define how reserves are
labelled and how many per cluster. This gates tenant isolation of reputation during a swap.

## Answer

**Reserve pool is per-cluster AND per-tenant.** `IMPLEMENTED` on branch
`feature/week2-4.1-tenant-sending`.

- `deliverability_sentinel._quarantine_and_swap` now promotes only a reserve whose
  `cluster_label IS NOT DISTINCT FROM` the degraded domain's cluster **and**
  `client_id IS NOT DISTINCT FROM` the degraded domain's client_id. `IS NOT DISTINCT FROM`
  makes the internal pool (`client_id`/`cluster_label` NULL) match its own reserves rather
  than any tenant's.
- No same-cluster reserve → log an error and leave the domain quarantined; **never** grab a
  foreign-cluster/tenant reserve (that would bleed one tenant's warmed reputation into
  another — the Task 4.1 isolation invariant).
- The sweep query now also selects `client_id` so the swap can filter on it.
- Code now matches the module docstring's "same-cluster" claim (previously contradicted it).
- **Provisioning dependency**: reserves must be labelled with their cluster + client_id at
  provisioning time — folded into ticket 03 (DNS/domain provisioning runbook). Reserve count
  per cluster to be set there (recommend ≥1 warm reserve per 3-domain cluster).
- No dedicated sentinel unit test existed; syntax + import verified. Live behaviour covered
  by the pre-pilot verification (ticket 04, DoD: 4% bounce → same-cluster swap in 5 min).

## ⚠ REOPENED 2026-09-07 — conflicts with the client's supplied reserve model

The client-supplied domain inventory
(`.scratch/task-3.1-outbound-sequencer/SENDING-DOMAINS.md`, 2026-09-03) lists **3 GLOBAL
reserves** — `equitypulsepartners.com`, `capitalreachhq.com`, `trusteecompass.com` — "warmed,
never sent from," belonging to **no client** (`client_id` NULL, `cluster_label` NULL).

My implementation promotes only a reserve with `client_id IS NOT DISTINCT FROM` the degraded
domain's client_id. A Pool B/C client domain (client_id = a real client) that trips would find
**no matching reserve** (the 3 reserves are global) → domain stays quarantined, **no swap**.
Implementation and client supply disagree.

This is the §7.3-vs-§7.4 design conflict the sequencer scratch already flagged: §7.3 forbids
cross-client identity pooling; §7.4 says promote a reserve on quarantine — but a reserve owned
by no client is in no cluster.

**Decision required (client/CEO — parked for relay):**
- **(a) Global reserves** — any client fails over to the shared warm pool. Matches the supplied
  3 reserves; but a promoted reserve then carries mixed-tenant reputation → weakens isolation.
  Revert the code to "any available reserve".
- **(b) Per-cluster reserves** — each client cluster keeps its own reserve (needs ~1 per
  cluster = 4 more domains at 4 clusters, not 3 global). My current code is correct; the
  client's supply must change.

Must be settled BEFORE the sentinel runs in the pilot, or a quarantine silently fails to swap.
Also links the open "4 vs 5 clients / domain shortfall" question (see CEO asks).
