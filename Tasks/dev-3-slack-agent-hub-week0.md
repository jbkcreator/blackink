# Dev 3 — Slack Agent Hub (@Blackink) · Week 0 Implementation Plan

**Sprint:** Week 0 (Aug 31 – Sept 2, 2026)
**Scope authority:** `Tasks/blackink_week0_sprint.md` → "Dev 3 — Slack Agent Hub (@Blackink)", items 1–5
**Acceptance:** AC #1 (owned), AC #3 (shared with Dev 2)
**Repo baseline:** branch `initialsetup`, Dev 1's landed structure
**Fork source:** `C:\Users\Sarath\Documents\ForcedAction-System\Forced-action-` (referred to below as `FA/`)

Where the sprint doc and the blueprint disagree, **the sprint doc wins** — every such
divergence is flagged inline as `⚠ DIVERGENCE`.

---

## 1. Scope & ownership

### 1.1 What Dev 3 owns

| # | Sprint item | Deliverable |
|---|---|---|
| 1 | Port async execution harnesses | Work-order queue, runner CLI, dispatch seam (`src/services/work_orders/`) |
| 2 | Port Slack interactive state machines | Signature verification, payload parsing, single-URL dispatcher (`block_actions` + **`view_submission`** + `view_closed`), card posting/updating, modal open **and submit** (§4.3a) |
| 3 | Stand up @Blackink app + 6 channels | Slack app manifest, channel registry, one app invited to all six |
| 4 | Payload-bound hash verification | `src/services/slack/payload_hash.py` + enforcement in the click path — **this is what AC #1 is judged on** |
| 5 | Cryptographic resume command — joint with Dev 2 | Slack-side halt/resume command surface against Dev 2's frozen interface |

### 1.2 What Dev 3 explicitly does NOT own

- **The halt persistence layer itself.** Sprint doc, Dev 2 §B: Dev 2 owns Redis + Postgres halt
  persistence and the removal of TTL auto-resume. Dev 3 builds only the Slack command/button that
  *calls* it, against the contract frozen in §6 below. Dev 3 must not write `execution_halts` DDL.
- **`EntityLeaseManager` (blueprint §5.3, `src/core/orchestration/lease_manager.py`) — NOT Week 0.**
  Verified by grep: "lease" and "lock" appear **nowhere** in the blueprint's Week 0 section
  (§3.0, lines 99–199). Every mention is in §5 — the nine-agent architecture (lines 1375, 1404,
  1734–1742), which describes the system at full build, not this sprint. It is also not one of item 1's
  three named things ("queue runners, dispatchers, and state machines"), and no acceptance criterion
  needs it: with one producer (`--seed`) there is nothing to contend with. Design notes and the two
  defects in the blueprint's sample code are preserved in the §3.3a appendix for whoever builds it in
  Week 1 — **do not build it in Week 0.**
- **Per-tenant channels `#client-{name}-growth` / `#client-{name}-launch`** (blueprint §5.1 topology).
  Week 3 Launch Agent scope. ⚠ DIVERGENCE: blueprint §5.1 lists 8 channels; sprint doc and blueprint
  §3.0.3 prose list 6. Sprint wins. The channel registry (§7.2) is shaped so these can be added as
  rows later without a code change, but no per-tenant channel is created in Week 0.
- **Cora queue throttling thresholds** (50 unreviewed / 24h age). Sprint doc assigns this to Dev 2 §C.
  Dev 3 exposes the queue depth Dev 2's throttle reads; it does not implement the pause decision.
- **Cross-tenant leakage CI tests.** Sprint doc, Dev 4 item 4.

### 1.3 Acceptance criteria, restated as testable claims

**AC #1** (blueprint §3.0.6.1): the @Blackink app is live in the workspace, posts native action cards,
processes interactive button clicks, and **rejects a click whose underlying payload changed since the
card was posted**.

**AC #3** (blueprint §3.0.6.3, shared): an emergency pause triggered via Slack persistently halts
background execution queues across server restarts until an authorized resume is executed. Dev 3's
half: the halt command authorizes correctly (fail closed), and the resume cannot be replayed against
a newer halt.

---

## 2. Folder structure

Laid over the existing `initialsetup` tree. Only files Dev 3 adds or touches are shown.

```
blackink/
├── config/
│   ├── settings.py                        [MODIFY]  + 6 channel IDs, approver allowlists
│   ├── redis_keys.py                      [MODIFY]  + global_key() for non-tenant halt scope
│   ├── tenant_policies.py                 [MODIFY]  + agent_work_orders entry
│   └── slack_channels.py                  [NEW]     channel registry (6 rows)
├── migrations/
│   └── apply_agent_work_orders.py         [NEW]     [FORK: FA/migrations/apply_relay_approval_queue.py]
├── src/
│   ├── api/
│   │   ├── main.py                        [MODIFY]  mount slack_router
│   │   └── slack_router.py                [NEW]     [FORK: FA/src/api/admin_router.py:1637-1671]
│   ├── core/
│   │   ├── models.py                      [MODIFY]  + AgentWorkOrder declarative model
│   │   └── (orchestration/lease_manager.py — NOT Week 0, see §1.2 + §3.3a appendix)
│   └── services/
│       ├── halt.py                        [DEV 2 OWNS — contract consumed here, see §6]
│       ├── work_orders/
│       │   ├── __init__.py                [NEW]     [FORK: FA/src/services/relay/queue.py]
│       │   ├── __main__.py                [NEW]     [FORK: FA/src/services/relay/__main__.py:225-277]
│       │   └── dispatchers.py             [NEW]     noop only in Week 0; email lands Week 1
│       └── slack/
│           ├── __init__.py                [NEW]
│           ├── auth.py                    [NEW]     [FORK: FA/admin_router.py:1602-1621 + 1781-1820]
│           ├── payload_hash.py            [NEW]     no FA equivalent — net new, AC #1 core
│           └── post.py                    [NEW]     [FORK: FA/src/services/relay/slack_post.py
│                                                     + FA/src/services/cora_throughput/batch_slack.py]
├── tests/
│   ├── helpers/slack_signing.py           [NEW]     [FORK: FA/tests/test_slack_interact_endpoint.py:14-30]
│   ├── test_payload_hash.py               [NEW]
│   ├── test_slack_interact.py             [NEW]     [FORK: FA/tests/test_relay_slack_endpoints.py]
│   ├── test_work_orders.py                [NEW]     [FORK: FA/tests/test_relay_queue.py]
│   └── test_slack_halt_command.py          [NEW]     joint with Dev 2
└── requirements.txt                       [MODIFY]  + slack_sdk
```

### 2.1 Why each new module exists (reuse justification)

| New file | Why an existing module does not fit |
|---|---|
| `src/api/slack_router.py` | Cannot extend `akrash_ingest_router.py`: different auth model entirely (Slack HMAC over the raw body vs. `get_current_akrash` JWT), different prefix, and Slack requires a *single* Interactivity Request URL that must not sit behind a JWT dependency. |
| `src/services/slack/auth.py` | Two functions, one concern ("is this inbound Slack request allowed"): request authenticity (HMAC) + user authorization (allowlist). FA splits these across `admin_router.py:1602` and `:1781`; merging them into one small module is fewer files with no loss of clarity. |
| `src/services/slack/payload_hash.py` | **No FA equivalent exists** — FA's button values carry `{item_id, action}` only (`FA/relay/slack_post.py:61-62`), with zero binding to message content. This is net-new work required by blueprint §3.0.3 / §5.1 "Payload-Bound Approvals". Its own module because it is imported by both the poster and the click handler, and is the single thing AC #1 turns on. |
| `src/services/slack/post.py` | Outbound Slack Web API calls, called by non-HTTP code paths (background jobs posting cards). Keeping it out of the router is what lets Dev 2's throttle and Week 1's Campaign Agent post cards without importing a FastAPI module. |
| `src/services/work_orders/` | Sits at `services/` level, **not** under `slack/`: blueprint §5.3 makes work orders the substrate for all nine agents, of which Slack is one consumer. Mirrors FA's own boundary — `relay/queue.py` lives outside the Slack module. |
| `config/slack_channels.py` | A registry, not settings. `settings.py` holds the env-backed channel *IDs*; this holds the fixed vocabulary (channel key → purpose → which env var supplies its ID), same split FA uses between `config/settings.py` and `relay/config.py` (`FA/relay/config.py:1-6` states this rationale explicitly). |

---

## 3. Subtask 1 — Port async execution harnesses (work-order queue)

### 3.1 Goal
Land `agent_work_orders` and the single read/write seam over it, so a card posted to Slack has a
durable row behind it whose status transitions are race-safe.

### 3.2 Forced Action source & fork classification

| FA source | Lines | Class | What changes on the way over |
|---|---|---|---|
| `FA/src/services/relay/queue.py` | 1–393 (whole module) | **REFACTOR** | `venture_key` → `client_id` (required, not defaulted); table `relay_approval_queue` → `agent_work_orders`; status vocabulary widened per blueprint §5.3; every read/write goes through `get_db_context(client_id=...)` so RLS applies. |
| `FA/src/services/relay/__main__.py` | 1–277 | **REFACTOR** | The **queue-runner CLI** — sprint doc item 1 says "port queue runners", and this is the runner. Fork `--health` / `--seed` / `--sweep` as `python -m src.services.work_orders`. FA's own rationale for building the runner before its producer exists (`FA/relay/queue.py:10-13`: the `--seed` CLI "calls this exact function today to build/prove the engine — identical schema and call shape, zero change when Cora lands") applies verbatim here: Week 0 has no Campaign Agent to enqueue work orders, so `--seed` is how AC #1 gets demonstrated end-to-end at all. Drop `--venture`; add `--client-id` (required, no default — an unscoped runner cannot write under RLS). |
| `FA/src/services/relay/config.py` | 22–28 (status constants) | **DIRECT FORK** | Constant names kept; values re-mapped to blueprint §5.3's `status` vocabulary. |
| `FA/src/services/relay/config.py` | **43** (`KILL_OVERRIDE_TTL_SECONDS = 3600`) | **FORK WITHOUT THIS** | See §3.6. |
| `FA/migrations/apply_relay_approval_queue.py` | 1–107 | **REFACTOR** | Idempotent-DDL shape and `--dry-run` flag kept verbatim; columns replaced with blueprint §5.3's. |
| `FA/src/agents/cora/queue.py` | 1–209 (Redis Streams) | **PATTERN ONLY** | Not ported. Blackink's Week 0 queue is Postgres-backed (a card must survive a Redis flush — Redis Streams would make the approval queue as volatile as the FA kill switch this sprint exists to fix). Adopt only the idempotency-key discipline (`FA/cora/queue.py:53-55`) and the DLQ concept, deferred. |
| `FA/src/agents/cora/worker.py` | 55–79 (`_already_processed` / `_record_processed`) | **PATTERN ONLY** | The "mark processed only after success, never on attempt" rule is adopted as a review rule for status transitions; no code ported. |
| `FA/src/services/action_queue.py` | 1–239 | **PATTERN ONLY** | Read-time union over three tables with no persistence. Rejected as a model: Blackink needs a real durable row per work order (blueprint §5.3), not a derived view. |

### 3.3 Files to create/change

**`migrations/apply_agent_work_orders.py`** [NEW]

```
DDL (idempotent, CREATE TABLE IF NOT EXISTS):

CREATE TABLE IF NOT EXISTS agent_work_orders (
    action_id           UUID          PRIMARY KEY DEFAULT gen_random_uuid(),
                                      -- ^ default is a SAFETY NET for direct/manual inserts only.
                                      -- work_orders.enqueue() MUST generate action_id in application
                                      -- code (uuid4) and pass it explicitly: action_id is inside the
                                      -- hash preimage (§5.2), so the digest cannot be computed before
                                      -- INSERT if the DB assigns the id. Same reasoning as CLAUDE.md's
                                      -- company_id rule (computed in app code, never gen_random_uuid()).
    client_id           VARCHAR(40)   NOT NULL REFERENCES clients(client_id),
    entity_type         VARCHAR(30)   NOT NULL,
    entity_id           VARCHAR(64)   NOT NULL,
    opportunity_id      VARCHAR(64),
    agent_id            VARCHAR(50)   NOT NULL,
    action_class         VARCHAR(100)  NOT NULL,
    autonomy_band        VARCHAR(20)   NOT NULL,
    risk_class           VARCHAR(20)   NOT NULL,
    confidence_score     NUMERIC(5,2),
    recipient            TEXT,
    payload              JSONB         NOT NULL DEFAULT '{}'::jsonb,
    config_fingerprint   JSONB         NOT NULL DEFAULT '{}'::jsonb,
    payload_hash         VARCHAR(64)   NOT NULL,
    hash_version         SMALLINT      NOT NULL DEFAULT 1,
    status               VARCHAR(20)   NOT NULL DEFAULT 'QUEUED',
    idempotency_key      VARCHAR(160)  NOT NULL,
    slack_channel_id     VARCHAR(30),
    slack_message_ts     VARCHAR(30),
    decided_by           VARCHAR(120),
    decided_at           TIMESTAMPTZ,
    execution_receipt    JSONB,
    error                TEXT,
    due_at               TIMESTAMPTZ,
    created_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    updated_at           TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT uq_agent_work_orders_idem UNIQUE (client_id, idempotency_key),
    CONSTRAINT ck_agent_work_orders_status CHECK (status IN
        ('QUEUED','APPROVED','REJECTED','SNOOZED','SKIPPED','DONE','EXECUTING','FAILED')),
    CONSTRAINT ck_agent_work_orders_band CHECK (autonomy_band IN
        ('BAND_1_OBSERVE','BAND_2_ONE_TAP','BAND_3_AUTO')),
    CONSTRAINT ck_agent_work_orders_risk CHECK (risk_class IN
        ('LOW','MEDIUM','HIGH','CRITICAL'))
);

CREATE INDEX IF NOT EXISTS ix_awo_client_status  ON agent_work_orders (client_id, status);
CREATE INDEX IF NOT EXISTS ix_awo_entity         ON agent_work_orders (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_awo_due            ON agent_work_orders (status, due_at);
GRANT SELECT, INSERT, UPDATE ON agent_work_orders TO blackink_app;
GRANT SELECT, INSERT, UPDATE ON agent_work_orders TO blackink_system;
```

⚠ **DIVERGENCE from blueprint §5.3 DDL, three points — repo convention wins:**
1. Blueprint declares `client_id UUID NOT NULL REFERENCES companies(company_id)`. That reference is
   wrong on its face — `client_id` is not a company id. Blackink's landed schema uses
   `VARCHAR(40) REFERENCES clients(client_id)` (see `src/core/models.py:409-419`, `Event`). Follow the
   repo. Same class of blueprint-SQL error CLAUDE.md already documents for `company_id`.
2. Blueprint has no `idempotency_key`. Added — blueprint §5.1's own Idempotency Engine row mandates
   one, and without it a retried enqueue silently duplicates a card. `UNIQUE (client_id,
   idempotency_key)` not global-unique, so two tenants cannot collide or probe each other's keyspace.
3. Blueprint has no `config_fingerprint` / `hash_version` / `entity_type`. Added: the first two are
   required by the hash design (§5), `entity_type` because `entity_id` alone is ambiguous across
   companies/contacts/campaigns.

**`config/tenant_policies.py`** [MODIFY] — add, or the table gets neither RLS nor leakage coverage
(CLAUDE.md: "there is no automatic net"):
```
"agent_work_orders": {"mode": "direct", "column": "client_id"},
```
Then re-run `migrations/apply_rls_policies.py` (it must run last, per CLAUDE.md).

**`src/core/models.py`** [MODIFY] — add `AgentWorkOrder(Base)` mirroring the DDL. Schema source of
truth per CLAUDE.md; runtime queries still use `text()`.

**`src/services/work_orders/__init__.py`** [NEW] — signatures:

```python
@dataclass(frozen=True)
class WorkOrder:
    action_id: str; client_id: str; entity_type: str; entity_id: str
    opportunity_id: str | None; agent_id: str; action_class: str
    autonomy_band: str; risk_class: str; confidence_score: Decimal | None
    recipient: str | None; payload: dict; config_fingerprint: dict
    payload_hash: str; hash_version: int; status: str; idempotency_key: str
    slack_channel_id: str | None; slack_message_ts: str | None
    decided_by: str | None; decided_at: datetime | None
    execution_receipt: dict | None; error: str | None
    due_at: datetime | None; created_at: datetime; updated_at: datetime

def enqueue(*, client_id: str, entity_type: str, entity_id: str, agent_id: str,
            action_class: str, autonomy_band: str, risk_class: str,
            payload: dict, config_fingerprint: dict, idempotency_key: str,
            recipient: str | None = None, opportunity_id: str | None = None,
            confidence_score: Decimal | None = None,
            due_at: datetime | None = None) -> WorkOrder: ...
    # ORDER MATTERS, and it is not the obvious one:
    #   1. action_id = str(uuid.uuid4())          — in app code, NOT the DB default
    #   2. payload_hash = payload_hash.compute(...) over the fully-populated preimage
    #      (action_id included — hence step 1 first)
    #   3. INSERT both
    # On UNIQUE violation returns the existing row — never a duplicate.
    # Forked from FA/relay/queue.py:109-180 (same IntegrityError→get-existing shape).

    # idempotency_key, when the caller has no better key, follows blueprint §5.1:
    #   sha256(f"{client_id}|{entity_id}|{action_class}|{timestamp_bucket}")
    # where timestamp_bucket is the UTC hour (YYYYMMDDHH). Callers with a natural
    # key (a campaign step, a reply id) should pass that instead — the formula is
    # the fallback, not a mandate.

def get(client_id: str, action_id: str) -> WorkOrder | None: ...
def get_by_idempotency_key(client_id: str, idempotency_key: str) -> WorkOrder | None: ...

def set_slack_message(client_id: str, action_id: str, *, channel_id: str,
                      message_ts: str) -> None: ...
    # Forked from FA/relay/queue.py:205-216. Stores channel_id too — FA stored
    # only ts and had to re-resolve the channel at edit time
    # (FA/admin_router.py:1822-1856 documents that a ts from one channel cannot
    # be edited in another). Storing both removes that whole failure mode.

def record_decision(client_id: str, action_id: str, *, decision: str,
                    decided_by: str) -> WorkOrder | None: ...
    # decision ∈ {APPROVED, REJECTED, SNOOZED, SKIPPED, DONE}
    # UPDATE ... WHERE action_id = :id AND client_id = :cid AND status = 'QUEUED'
    # Returns None when rowcount == 0 (already decided / double-tap / Slack retry).
    # Direct fork of FA/relay/queue.py:218-245 — that guard is the double-tap fix.

def update_payload(client_id: str, action_id: str, *, payload: dict,
                   config_fingerprint: dict) -> WorkOrder: ...
    # The ONLY sanctioned way to mutate a QUEUED order's content. Recomputes and
    # persists payload_hash in the SAME transaction, so no caller can leave a
    # stale hash behind. See §5.5 (rotation).

def queued_depth(client_id: str | None = None,
                 *, older_than: timedelta | None = None) -> int: ...
    # What Dev 2 §C's throttle reads (50-count and 24h-age triggers).
    # client_id=None aggregates via get_system_db_context() — batch use only.

def snooze(client_id: str, action_id: str, *, until: datetime,
           decided_by: str) -> WorkOrder | None: ...
```

**`src/services/work_orders/__main__.py`** [NEW] [FORK: `FA/src/services/relay/__main__.py:225-277`]
— the queue runner, without which there is nothing to demo AC #1 against (Week 0 has no agent
producing work orders yet):
```
# _platform_internal is the reserved internal client_id Dev 1 already seeds
# (migrations/apply_clients.py:96-100, SEED_INTERNAL_CLIENT_SQL) — Blackink's own
# self-marketing tenant. Do NOT invent another; it is an FK into clients.
python -m src.services.work_orders --health  --client-id _platform_internal
python -m src.services.work_orders --seed    --client-id _platform_internal \
        --action-class DISPATCH_EMAIL_TOUCH --entity-type contact --entity-id <id> \
        --recipient a@b.com --payload-json '{"subject":"...","body":"..."}' \
        --channel-key setter
python -m src.services.work_orders --sweep   --client-id _platform_internal
```
- `--health` — scaffolding check: DB reachable, `agent_work_orders` present, RLS forced, Slack
  configured, channel IDs resolvable. Forked from `cmd_health` (`FA/relay/__main__.py:59-115`).
- `--seed` — enqueue one work order and post its card. This is the AC #1 demo driver.
- `--sweep` — claim `APPROVED` orders and hand them to a registered dispatcher. In Week 0 the only
  registered dispatcher is `noop` (records an `execution_receipt`, sends nothing); the real email
  dispatcher lands in Week 1. Registering `noop` now is what proves the state machine
  `QUEUED → APPROVED → EXECUTING → DONE` actually closes.
- **Halt check before the batch and before every item**, per `FA/relay/engine.py:112-131` — calls
  `halt.is_halted(...)` (§6.2). This is the enforcement point AC #3 is judged at; without it the halt
  is a database row nothing reads.

⚠ `--sweep` must refuse to run without `--client-id`. FA defaulted to venture #1
(`FA/relay/__main__.py` uses `DEFAULT_VENTURE_KEY`); under FORCE'd RLS an unscoped sweep silently
returns zero rows, which looks like "nothing to do" rather than a misconfiguration.

### 3.3a APPENDIX — `EntityLeaseManager`: **DO NOT BUILD IN WEEK 0**

> **Out of Week 0 scope** (§1.2). Kept here only so the Week 1 developer inherits the defect analysis
> instead of re-discovering it. Nothing in this subsection is a Week 0 deliverable.

Blueprint §5.3 supplies working code for `src/core/orchestration/lease_manager.py`. When it is built,
fork it **not verbatim** — the sample has two real bugs:

1. **`release_entity_lease` is not atomic.** It does `GET` → compare `agent_id` → `DELETE` as three
   round trips. Between the compare and the delete, the lease can expire and be re-acquired by another
   agent, and this call then deletes *that* agent's lease. Fix with a Lua CAS (or
   `SET ... XX GET` + conditional delete in one script):
   ```
   if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) else return 0 end
   ```
2. **No owner check on acquire, and no re-entrancy.** `SET NX` returns False for the *same* agent
   re-acquiring its own live lease, so a retried job deadlocks against itself. Return
   `ALREADY_HELD_BY_SELF` distinctly from `HELD_BY_OTHER`.

A TTL **is** correct here, unlike on a halt (§6.3 invariant 1) — a lease must expire or a crashed agent
wedges an entity forever. Keep blueprint §5.3's 300s. State the distinction in the module docstring so
nobody "fixes" the lease TTL away by analogy with the halt work:

> A lease TTL is a liveness guarantee (a dead holder must release). A halt TTL is a safety violation
> (a dead operator must NOT release). Same mechanism, opposite intent.

```python
# src/core/orchestration/lease_manager.py   [NEW] [FORK: blueprint §5.3, defects fixed]

LEASE_TTL_SECONDS = 300

def acquire_entity_lease(client_id: str, entity_id: str, agent_id: str,
                         ttl_seconds: int = LEASE_TTL_SECONDS) -> LeaseResult: ...
    # LeaseResult ∈ {ACQUIRED, ALREADY_HELD_BY_SELF, HELD_BY_OTHER}
def release_entity_lease(client_id: str, entity_id: str, agent_id: str) -> bool: ...
    # Lua CAS — releases only if this agent still holds it.
def lease_holder(client_id: str, entity_id: str) -> str | None: ...
```

Its first real caller is the Week 1 Campaign Agent — the first point at which two agents can act on
one entity concurrently, which is the race the lease exists to prevent.

⚠ When built, do **not** use blueprint §5.3's key shape `f"lease:{client_id}:{entity_id}"` — that puts
`client_id` in the *middle*. Go through `tenant_redis`/`client_key()`, which puts it first
(`{client_id}:lease:{entity_id}`): CLAUDE.md requires the tenant prefix be structurally impossible to
omit, and tenant teardown by `SCAN {client_id}:*` (blueprint §5.2 Launch Agent offboarding) only works
with the tenant leading.

### 3.4 Redis keys

**None in Week 0.** Work-order state is Postgres-only, deliberately — see the `cora/queue.py`
PATTERN-ONLY note in §3.2. The only Redis this workstream touches is the halt fast-path, which Dev 2
owns (§6.5).

### 3.5 Build order
1. `apply_agent_work_orders.py` + `models.py` entry.
2. `tenant_policies.py` entry, re-run `apply_rls_policies.py`, confirm its fail-loud verification passes.
3. `work_orders/__init__.py` — `enqueue` / `get` / `record_decision` first; `update_payload` after §5 lands.
4. `work_orders/dispatchers.py` (noop) + `work_orders/__main__.py` (`--health`/`--seed`/`--sweep`).
5. Add the migration line to CLAUDE.md's ordered migration list (before `apply_rls_policies.py`).

### 3.6 Defect NOT ported
`FA/src/services/relay/config.py:43` — `KILL_OVERRIDE_TTL_SECONDS = 3600`, consumed at
`FA/admin_router.py:2288` as `rset(..., ttl_seconds=KILL_OVERRIDE_TTL_SECONDS)` and advertised to the
operator at `:2291` as *"Auto-clears on expiry."* Blackink Week 0 §3.0.2-B exists specifically to kill
this: a halt that re-arms on a timer means the machine overrides a human's stop. Do not port the
constant, the TTL argument, or the message. Fork the surrounding `/slack/kill` endpoint shape
(`FA/admin_router.py:2253-2287`) **without** those three lines. Details in §6.

### 3.7 Tests
- `tests/test_work_orders.py` [FORK: `FA/tests/test_relay_queue.py`] — FakeSession pattern per
  `tests/test_compliance_gate.py:54-64`. No live DB.
  - `enqueue` twice with the same `(client_id, idempotency_key)` → one row, second call returns first.
  - `record_decision` on a non-`QUEUED` row → `None` (double-tap guard).
  - `record_decision` with a mismatched `client_id` → `None`.
  - `update_payload` recomputes the hash (assert the digest changes).
- **Needs live Postgres:** the RLS assertion — a session scoped to client A cannot read client B's
  work order. Add to `tests/test_tenant_isolation.py`, which already requires a live DB and is what
  the nightly workflow runs.

### 3.8 Done when
- [ ] Migration applies twice in a row with no error (idempotent) and `--dry-run` reports correctly.
- [ ] `apply_rls_policies.py` verification passes with `agent_work_orders` included.
- [ ] `enqueue` is idempotent on `(client_id, idempotency_key)`.
- [ ] `record_decision` returns `None` for any non-`QUEUED` row.
- [ ] Cross-tenant read returns zero rows under RLS (live-Postgres test).
- [ ] No `KILL_OVERRIDE_TTL_SECONDS` equivalent anywhere in the diff.
- [ ] `--health` reports green; `--seed` enqueues and posts a card; `--sweep` drives
      `QUEUED → APPROVED → EXECUTING → DONE` against the `noop` dispatcher.
- [ ] `--sweep` refuses to run without `--client-id`.
- [ ] `--sweep` checks `halt.is_halted()` before the batch and before every item.

---

## 4. Subtask 2 — Port Slack interactive state machines

### 4.1 Goal
One signature-verified Slack Interactivity URL that dispatches on `action_id`, plus the outbound
helpers to post, update, and open modals on cards.

### 4.2 Forced Action source & fork classification

| FA source | Lines | Class | What changes |
|---|---|---|---|
| `admin_router.py:_verify_slack_signature` | 1602–1621 | **DIRECT FORK** | Move to `src/services/slack/auth.py`. Logic unchanged — HMAC-SHA256 over `v0:{ts}:{body}`, 300s replay window, `hmac.compare_digest`. Read the secret via `get_settings()` rather than a module-level `settings` binding: FA's own tests document that module-binding causing desync (`FA/tests/test_slack_interact_endpoint.py:50-61`). |
| `admin_router.py:_slack_ephemeral` | 1623–1625 | **DIRECT FORK** | Verbatim. |
| `admin_router.py:_parse_slack_interactive_payload` | 1627–1635 | **DIRECT FORK** | Verbatim, including the 400-on-malformed behavior. |
| `admin_router.py:slack_interact` | 1637–1671 | **REFACTOR** | Keep the single-URL insight verbatim — FA's docstring at 1643-1650 records that three features each registering their own Request URL meant "at most one of the three was ever actually reachable". Replace FA's hardcoded `if action_id == ...` chain and its `_CORA_EXACT`/`_CORA_PREFIXES` frozensets with a registry dict, and insert the hash check (§5) before any handler runs. |
| `admin_router.py:_relay_approver_authorized` | 1781–1820 | **REFACTOR** | Fork the fail-closed core verbatim — FA's docstring is explicit that `if approvers and user_id not in approvers` short-circuits to a no-op on an empty list, "silently accepting any Slack workspace member". Replace `venture_key` widening with `client_id` widening. |
| `admin_router.py:_handle_county_launch_interact` | 1684–1751 | **PATTERN ONLY** | Adopt: read row → `SELECT FOR UPDATE` → terminal-status check → audit row → strip buttons. Business logic is county-launch specific, not ported. |
| `admin_router.py:_update_slack_message` | 1753–1778 | **REFACTOR** | Generalize off `ExpansionCandidate` to `(channel_id, message_ts, text)`. |
| `admin_router.py:_update_relay_slack_message` | 1822–1866 | **PATTERN ONLY** | Its whole complexity is re-resolving the per-venture channel from a ts. Obsolete here: `set_slack_message()` stores `slack_channel_id` alongside `ts` (§3.3). |
| `admin_router.py:_handle_relay_decision` | 1868–1911 | **REFACTOR** | Fork the *intent* — authorization must be scoped to the item's own tenant (FA:1897-1901) — but **not** FA's read-row-first ordering: under RLS the button's `client_id` and the row's `client_id` are necessarily equal (§4.3 step 6 note), so Blackink authorizes first and avoids the DB read. Reject FA's fallback of defaulting an unknown id to venture #1's approver list: an unknown `action_id` in Blackink is rejected outright, never authorized against a default tenant. |
| `admin_router.py:_bg_batch_decision` | 1926–1978 | **PATTERN ONLY** | ACK Slack fast, do work in a background task. Adopted for card *refresh* only. |
| `relay/slack_post.py:post_for_approval` | 37–93 | **REFACTOR** | Keep WebClient usage, the JSON-string-in-button-`value` convention, and the no-op-when-unconfigured behavior (`:44-56`). Button value gains `payload_hash` + `client_id` (§5.3). |
| `relay/slack_post.py:post_completion_receipt` | 96–146 | **DIRECT FORK** | Receipt posting, never raises. |
| `cora_throughput/batch_slack.py:open_draft_modal` | 52–68 | **DIRECT FORK** | `views_open` + `trigger_id`; generalize the modal builder's input type. |
| `cora_throughput/batch_slack.py:_build_view_modal` | 34–50 | **REFACTOR** | Keep the 3000-char body truncation. |
| `lifecycle_slack.py:post_incident_alert` | 106–175 | **REFACTOR** | Adopt the never-silently-fail rule — Slack-unconfigured falls back rather than dropping the alert. For Blackink the fallback is an `events` row, not email (no SMTP configured; an unroutable alert must still be durably recorded). |
| `relay/engine.py` | 1–183 | **PATTERN ONLY** | Execution engine, Week 1 Campaign Agent scope. Adopt now only its halt-check-before-every-item rule as a review rule. |
| `relay/sweep.py` | 1–88 | **PATTERN ONLY** | Cron sweep, not Week 0. |
| `relay/guards.py:reserve_daily_slot` | 106–134 | **PATTERN ONLY** | The **fail-closed-on-Redis-outage** reasoning (`:113-125`) is the pattern to carry into §6's halt read: an unreachable Redis must read as halted, never as clear. |

### 4.3 Files to create/change

**`src/services/slack/auth.py`** [NEW]
```python
def verify_slack_signature(headers: Mapping[str, str], body: bytes) -> bool: ...
    # DIRECT FORK of FA/admin_router.py:1602-1621.

def approver_authorized(user_id: str, *, client_id: str | None = None) -> bool: ...
    # Fail CLOSED. Empty/unset allowlist ⇒ nobody authorized.
    # Fleet list (settings.blackink_global_approvers) checked first — no DB read,
    # no cache skew (FA/admin_router.py:1804-1812 reasoning).
    # client_id widens with that tenant's own approvers; both empty ⇒ False.
    # A valid Slack signature proves the request came from Slack for this app —
    # it says NOTHING about which workspace member sent it (FA:1786-1790).
```

**`src/services/slack/post.py`** [NEW]
```python
def post_action_card(order: WorkOrder, *, channel_key: str,
                     buttons: Sequence[str], blocks: list | None = None) -> str | None: ...
    # Posts, then calls work_orders.set_slack_message(channel_id=..., message_ts=...).
    # Every button's value is built by payload_hash.button_value(order, decision) (§5.3).
    # Returns ts, or None when Slack is unconfigured (no-op + log, FA/relay/slack_post.py:44-56).

def update_card(client_id: str, *, channel_id: str, message_ts: str,
                text: str) -> bool: ...
    # Strips the actions block, replaces with outcome text. Never raises.

def open_modal(trigger_id: str, view: dict) -> bool: ...
def post_notice(channel_key: str, text: str, blocks: list | None = None) -> str | None: ...
    # Non-interactive posts (#blackink-qa health, #blackink-economics rollups).
```

**`src/api/slack_router.py`** [NEW]
```python
router = APIRouter(prefix="/api/slack", tags=["slack"])

# The ONE Interactivity Request URL. Slack permits exactly one per app.
@router.post("/interact")
async def slack_interact(request: Request, background_tasks: BackgroundTasks): ...

# The ONE slash-command URL; dispatches on the `command` form field.
@router.post("/command")
async def slack_command(request: Request): ...

# Work-order actions. TERMINAL = the click decides the order and the card is closed out.
# NON-TERMINAL = the click opens a modal; the card stays live until the modal submits
# (or the user cancels), so the terminal steps 9-10 below MUST NOT run.
WORK_ORDER_ACTIONS: dict[str, ActionSpec] = {
    "approve":   ActionSpec(decision="APPROVED", terminal=True),
    "reject":    ActionSpec(decision="REJECTED", terminal=True),
    "skip":      ActionSpec(decision="SKIPPED",  terminal=True),
    "mark_done": ActionSpec(decision="DONE",     terminal=True),
    "snooze":    ActionSpec(decision="SNOOZED",  terminal=True),   # static_select carries `until`
    "revise":    ActionSpec(decision=None,       terminal=False),  # opens modal → §4.3a
}

# Non-work-order actions. These have NO work order, so they skip steps 5-8 entirely.
CONTROL_ACTIONS: dict[str, Callable[[SlackClick], dict]] = {
    "halt_resume": ...,   # §6.4 — value is {kind, halt_id, resume_nonce}, not a work-order value
}
```
The six work-order `action_id`s are exactly the sprint doc's Dev 3 item 2 list and blueprint §3.0.3's
(`Approve`, `Revise`, `Reject`, `Snooze`, `Skip`, `Mark Done`).

**Request flow for `/interact`** — fixed order, each step failing closed:
1. `verify_slack_signature(headers, raw)` → 401 on failure.
2. `_parse_slack_interactive_payload(raw)` → 400 on malformed.
3. **Branch on `payload["type"]` BEFORE touching `actions`** (§4.3a): `block_actions` → step 4;
   `view_submission` → modal-submission path; `view_closed` → 200 no-op; anything else → ephemeral.
4. Extract `action_id` and `user.id`; parse `actions[0].value` JSON.
5. **Route on the action's family before interpreting the value** — the two families have different
   value shapes and different required steps:
   - `action_id in CONTROL_ACTIONS` → authorize (`approver_authorized(user_id)`, no `client_id`: a
     global halt has none), then hand the whole value to the control handler. **Skip steps 6–9** —
     there is no work order, no tenant scope, and no payload hash. Return.
   - `action_id in WORK_ORDER_ACTIONS` → continue at step 6 with
     `{client_id, action_id, decision, payload_hash}`.
   - neither → ephemeral `Unrecognized action`, no state change.
6. `approver_authorized(user_id, client_id=...)` → ephemeral "Not authorized" on failure.
7. Open `get_db_context(client_id=...)` — **tenant context comes from the button value**, see §4.4.
8. Load the work order scoped to that `client_id`. Not found → ephemeral (covers a tampered
   `client_id`: the scoped read simply returns nothing).
9. **`payload_hash.verify(order, provided_hash)`** → on `not fresh`, ephemeral rejection (§5.5) and
   stop; on `not integrity_ok`, also alert `#blackink-qa` (§5.4 check 2).
10. Status must be `QUEUED` → ephemeral "already decided" otherwise.
11. Dispatch to the handler for `action_id`.
12. **Only if `spec.terminal`:** `work_orders.record_decision(...)`, write an `events` row, and
    `update_card(...)` to strip the buttons. A non-terminal action (`revise`) instead calls
    `post.open_modal(...)` with `private_metadata` per §4.3a and leaves the card and its status
    untouched — the decision is recorded by the `view_submission` handler, not here.

⚠ Step 6 authorizes on the button's `client_id` *before* the row is read, whereas FA reads the row
first (`FA/admin_router.py:1897-1901`). This is not a divergence in effect: RLS guarantees a row
returned at step 8 has `client_id` equal to the session scope set at step 7, so authorizing on the
button value and authorizing on the row's tenant are the same check. Doing it first is strictly
better — an unauthorized click costs no DB read, and cannot be used to probe whether an `action_id`
exists. FA had to read first only because it had no RLS to make the two equivalent.

**`src/api/main.py`** [MODIFY] — `app.include_router(slack_router)`.

**`requirements.txt`** [MODIFY] — `slack_sdk>=3.27`.

### 4.3a Modal submission listeners — a real gap in the fork source

Sprint doc Dev 3 item 2 requires *"Block Kit button handlers **and modal submission listeners**"*
(blueprint §3.0.1, "Slack Interactive State Machines", direct-fork tier). **FA has no modal submission
handling at all** — `grep -rn "view_submission\|private_metadata" FA/src/` returns nothing. Its only
modal use is read-only: `FA/cora_throughput/batch_slack.py:51-68` (`views_open`) invoked from
`FA/admin_router.py:2029`, with no path for what the user submits back. Porting FA as-is delivers half
of item 2.

**Why it breaks the dispatcher, concretely:** Slack posts `view_submission` to the *same* Interactivity
URL, but that payload has **no `actions` array** — it carries `view.callback_id`,
`view.private_metadata`, and `view.state.values`. FA's dispatcher reads
`payload.get("actions", [])` and derives `action_id` from `actions[0]`
(`FA/admin_router.py:1653-1655`); a submission therefore falls through to
`_slack_ephemeral("Unrecognized action: None")` and the operator's input is silently discarded.

**Required in Week 0:**

```python
# src/api/slack_router.py

# Dispatched on payload["type"], not on action_id — a view_submission has no action_id.
async def slack_interact(request: Request, background_tasks: BackgroundTasks):
    ...
    ptype = payload.get("type")
    if ptype == "block_actions":   return _handle_block_action(payload, ...)
    if ptype == "view_submission": return _handle_view_submission(payload, ...)
    if ptype == "view_closed":     return Response(status_code=200)   # user hit Cancel
    return _slack_ephemeral(f"Unsupported interaction type: {ptype}")

VIEW_HANDLERS: dict[str, Callable[[SlackSubmission], dict]] = {
    "revise_work_order": ...,   # the Revise modal (§9 item 1)
    "snooze_work_order": ...,   # custom snooze-until, if the static select is insufficient
}
```

**Carrying context into the submission — `private_metadata`.** A `view_submission` payload contains no
reference to the message the modal was opened from, so the work-order context must be embedded in the
view when it is opened:

```python
view["private_metadata"] = json.dumps({
    "client_id":    order.client_id,
    "action_id":    str(order.action_id),
    "payload_hash": compute(order),   # hash AT MODAL-OPEN time
    "channel_id":   order.slack_channel_id,
    "message_ts":   order.slack_message_ts,
})
```

`private_metadata` is a 3000-char server-set string that Slack echoes back verbatim on submission, and
the whole request body remains covered by the Slack signature — so it is exactly as trustworthy as a
button `value`, and no more. Treat it identically: **re-run the full §5.4 hash verification on
submission**, not just at modal-open. A modal can sit open for minutes; the payload can change
underneath it in exactly the way §5 exists to catch. Verifying only at open would leave a hole the
button path doesn't have.

**Response contract** (Slack-specific, easy to get wrong): a `view_submission` must return HTTP 200
with either an empty body (close the modal), `{"response_action": "clear"}`, `{"response_action":
"update", "view": {...}}`, or `{"response_action": "errors", "errors": {block_id: message}}`. An
ephemeral-message dict — what every button handler returns — is **not** a valid `view_submission`
response and renders nothing. A stale-hash rejection at submission therefore returns
`{"response_action": "errors", "errors": {"revision_note": "This card is out of date — nothing was
saved. Close this and use the refreshed card."}}`, not `_slack_ephemeral(...)`.

**Tests** (add to `tests/test_slack_interact.py`):
- `view_submission` with a valid `private_metadata` → handler runs, returns 200 with a valid
  `response_action`.
- `view_submission` whose `private_metadata` hash is stale → `response_action: errors`, no state change.
- `view_submission` with an unknown `callback_id` → rejected, no state change.
- `view_closed` → 200, no state change.
- A `block_actions` payload and a `view_submission` payload hitting the same URL both dispatch
  correctly (the regression test for the FA gap).

### 4.4 The RLS chicken-and-egg (resolve this explicitly — it will bite otherwise)

Under FORCE'd RLS the app role cannot read a row without `app.current_client_id` already set, but
`client_id` normally lives *in* the row. Reading the row to learn its tenant is therefore impossible
on the app engine, and `get_system_db_context()` (BYPASSRLS) **must not** be used — CLAUDE.md forbids
importing it from `src/api/`, enforced by a CI grep-lint.

**Resolution:** `client_id` travels in the button `value` and is the tenant context for step 5. This
is safe on two independent grounds:
1. The entire request body — button value included — is covered by Slack's HMAC signature (step 1), so
   altering it requires forging the signing secret.
2. `client_id` is inside the hash preimage (§5.2), so a swapped `client_id` also fails step 7.

A wrong-but-well-formed `client_id` yields zero rows at step 6 and is rejected. No BYPASSRLS anywhere
in the request path.

### 4.5 Redis keys
None. Dedupe of Slack's own retries is handled by the `status = 'QUEUED'` guard in
`record_decision`, which is idempotent by construction — no separate Redis dedupe key needed.

### 4.6 Tests
- `tests/helpers/slack_signing.py` [FORK: `FA/tests/test_slack_interact_endpoint.py:14-30`] — the
  signed-request builder, with a `ts_offset` parameter for replay tests.
- `tests/test_slack_interact.py` [FORK: `FA/tests/test_relay_slack_endpoints.py`], no live DB:
  - Bad signature → 401. Missing signature → 401.
  - `ts_offset=-400` (>300s) → 401 replay rejection.
  - Malformed body → 400.
  - Unknown `action_id` → ephemeral, no state change.
  - Empty approver allowlist → "Not authorized" (**the fail-closed regression test — this is the FA
    bug at `admin_router.py:1786-1790`; it must have a test of its own**).
  - Non-approver user id → "Not authorized".
  - Happy path per `action_id` → `record_decision` called once, card updated once.
  - Double-tap: same click twice → second returns "already decided", `record_decision` called once.

### 4.7 Done when
- [ ] One URL serves all six `action_id`s; the registry has no hardcoded `if` chain.
- [ ] `/interact` dispatches on `payload["type"]` first — `block_actions`, `view_submission`, and
      `view_closed` all handled; a submission never falls through to "Unrecognized action".
- [ ] Modal submissions re-verify the hash from `private_metadata` and return a valid
      `response_action`, never an ephemeral dict.
- [ ] Bad/expired/absent signature is rejected before any DB access.
- [ ] Empty allowlist authorizes nobody (test present).
- [ ] No `src/api/` file imports `get_system_db_context` (CI grep-lint clean).
- [ ] Slack-unconfigured posts no-op and log rather than raising.

---

## 5. Subtask 4 — Payload-bound hash verification (AC #1)

> Blueprint §3.0.3: *"Every interactive Slack card is cryptographically bound to a SHA-256 hash of the
> exact message payload, recipient identifier, and configuration state. If a draft payload or template
> is modified in the background while awaiting review, clicking an outdated Slack button is rejected by
> the backend."* Blueprint §5.1 restates it as a Shared Agent Core invariant.

**No FA equivalent.** FA's button values carry `{item_id, action}` only
(`FA/relay/slack_post.py:61-62`) — a draft edited between post and click executes under the stale
approval with no detection. This section is net-new.

### 5.1 What is being defended against
An operator sees card content X and clicks Approve. Between post and click, a background job rewrote
the work order to content Y. Without binding, the click approves Y while the human approved X.

### 5.2 The canonical preimage — exact and unambiguous

Field set is **fixed and ordered**. Adding, removing, or reordering a field is a breaking change and
requires bumping `HASH_VERSION` (persisted per row as `hash_version`, so in-flight cards stay
verifiable across a deploy).

```
HASH_VERSION = 1

preimage_object = {
    "v":             1,                                  # int, = HASH_VERSION
    "client_id":     order.client_id,                    # str
    "action_id":     str(order.action_id),               # str, UUID canonical lowercase
    "action_class":  order.action_class,                 # str
    "entity_type":   order.entity_type,                  # str
    "entity_id":     order.entity_id,                    # str
    "recipient":     order.recipient or "",              # str — "recipient identifier"
    "payload":       _normalize(order.payload),           # dict — "exact message payload"
    "config":        _normalize(order.config_fingerprint) # dict — "configuration state"
}
```

**Serialization (byte-exact, no room for interpretation):**
```python
canonical = json.dumps(
    preimage_object,
    sort_keys=True,               # key order can never affect the digest
    separators=(",", ":"),        # no incidental whitespace
    ensure_ascii=False,           # UTF-8 text stays UTF-8
    allow_nan=False,              # NaN/Infinity would not round-trip — raise instead
).encode("utf-8")

digest = hashlib.sha256(canonical).hexdigest()   # 64 lowercase hex chars
```

**`_normalize(d: dict) -> dict`** — recursive, and deliberately minimal:
- `\r\n` and `\r` → `\n` in every string value.
- Strip trailing whitespace from each line of every string value.
- `Decimal` → `str`; `datetime` → ISO-8601 UTC with `Z`.
- Drop keys whose value is `None` (so an absent key and an explicit `null` hash identically —
  otherwise a JSONB round-trip through Postgres can flip one into the other and break a legitimate card).
- **Nothing else.** No case folding, no whitespace collapsing, no HTML stripping. Over-normalizing is
  the failure mode that matters here: it would let a real content change hash identically and defeat
  the whole control. Line-ending and trailing-space normalization is the minimum needed to survive a
  Slack/Postgres round-trip; everything beyond that weakens the guarantee.

**`config_fingerprint` contents** — the "configuration state" half of the blueprint's requirement.
Whatever the executor will actually read at send time must be in here, or a config swap goes
undetected:
```
{ "template_version": str, "channel": str, "sending_domain": str | None,
  "mailbox_id": str | None, "autonomy_band": str, "offer_row_key": str | None }
```

### 5.3 Where the digest lives

| Location | Purpose |
|---|---|
| `agent_work_orders.payload_hash` (+ `hash_version`) | Persisted at INSERT by `work_orders.enqueue`; re-persisted by `update_payload`. **Read only as an integrity tripwire (§5.4 check 2) — never as the value to put on a button.** |
| Slack button `value` JSON | `{"client_id": ..., "action_id": ..., "decision": ..., "payload_hash": ...}` — ~140 bytes, well inside Slack's 2000-char button-value limit. The payload itself is never in the button; only its digest. |

```python
def button_value(order: WorkOrder, decision: str) -> str: ...
    # json.dumps of the four fields above, with payload_hash = compute(order)
    # — RECOMPUTED, never order.payload_hash.
    #
    # Why: if a rogue writer mutated payload without updating the stored column,
    # a button built from the stored (now-wrong) column would carry a digest that
    # fails its own verification on the very first click — the refreshed card
    # posted by the §5.5 rejection path would itself be un-clickable, and every
    # retry would loop. Recomputing means a freshly-posted card is always
    # internally consistent; the stored column's only job is to trip the
    # integrity alarm in §5.4 check 2.
    # Used by post.post_action_card for every button on every card.

### 5.4 Recomputation at click time

```python
def compute(order: WorkOrder) -> str: ...
    # canonical preimage → sha256 hexdigest, per §5.2.

@dataclass(frozen=True)
class HashVerdict:
    fresh: bool             # check 1 — button hash matches current content
    integrity_ok: bool      # check 2 — stored column matches current content
    recomputed: str
    provided: str
    stored: str

def verify(order: WorkOrder, provided_hash: str) -> HashVerdict: ...
```

`verify` performs **two** distinct comparisons and **evaluates both unconditionally — it does not
short-circuit.** A rogue mutation trips both at once (the click is stale *and* the stored column has
drifted); short-circuiting on check 1 would report it as an ordinary stale click and swallow the
platform defect. The caller acts on `fresh` and alerts on `not integrity_ok`, independently.

1. **Freshness (the AC #1 check).** Recompute the digest from the order's *current* row state and
   compare to the hash carried in the button value:
   ```python
   recomputed = compute(order)
   fresh = hmac.compare_digest(recomputed, provided_hash)
   ```
   `hmac.compare_digest`, not `==`: constant-time, same discipline FA already applies to the request
   signature at `FA/admin_router.py:1620`.
   Recomputing — rather than comparing the button hash against the stored `payload_hash` column — is
   the point. Comparing against the column would pass whenever a writer mutated `payload` but failed
   to update `payload_hash`, which is precisely the bug class this exists to catch.

2. **Stored-column integrity.** Compare `recomputed` against the persisted `order.payload_hash`. A
   mismatch means a writer bypassed `update_payload()` and the row's own hash is stale. This is a
   platform defect, not a user error: reject the click, and post to `#blackink-qa` (blueprint §3.0.3
   assigns cross-tenant and integrity alerts to that channel), plus an `events` row of type
   `work_order_hash_integrity_failure`.

**Replay window.** Two independent layers, no third needed:
- Slack's own 300s signature timestamp window (`auth.verify_slack_signature`) bounds request replay.
- The `status = 'QUEUED'` guard in `record_decision` makes any re-delivery of an already-decided click
  a no-op. Together these cover replay; a separate nonce per card would add a table write per post for
  no additional coverage.

### 5.5 Rejection response

Ephemeral (visible only to the clicker, no channel noise), and it must say what to do next — a bare
"rejected" trains operators to re-click:

```
:warning: This card is out of date — the message or its configuration changed
after this card was posted, so the click was not executed.
Nothing was sent. A refreshed card has been posted below; review and act on that one.
```

Then: write an `events` row (`event_type="work_order_stale_click_rejected"`,
`entity_type="work_order"`, `entity_id=action_id`, `actor=f"slack:{user_id}"`, payload carrying
`provided_hash` and `recomputed_hash`) and re-post a fresh card. The stale click is a **hard reject**:
never fall through to "approve the current version anyway", which would silently execute content the
human never saw.

### 5.6 Rotation (how a legitimate change re-arms the card)

Single sanctioned path, so no caller can forget a step:

```
work_orders.update_payload(client_id, action_id, payload=..., config_fingerprint=...)
  ├─ one transaction: UPDATE payload, config_fingerprint, payload_hash = compute(...), updated_at
  ├─ guarded WHERE status = 'QUEUED'  (a decided order is immutable)
  └─ returns the new WorkOrder
then:
  post.update_card(...)   → old message becomes "superseded, see refreshed card"
  post.post_action_card(...) → new message, new ts, buttons carrying the NEW hash
```

Every previously-posted button for that order now carries a hash that no longer matches the
recomputed digest, so any late click on an old card is rejected by §5.4 step 1. That is the mechanism —
no cache, no expiry, no bookkeeping of old hashes.

### 5.7 Tests — `tests/test_payload_hash.py` [NEW], no live DB

- **Determinism:** same order → same digest across processes (assert against a hardcoded expected
  digest, so an accidental serialization change fails loudly rather than silently re-baselining).
- **Key order irrelevance:** `payload={"a":1,"b":2}` and `{"b":2,"a":1}` hash identically.
- **Sensitivity, one test per preimage field:** mutating each of `client_id`, `action_id`,
  `action_class`, `entity_type`, `entity_id`, `recipient`, `payload.body`, `payload.subject`, and each
  `config_fingerprint` key changes the digest. Nine-plus assertions — this is the table that proves
  coverage of "message payload, recipient identifier, and configuration state".
- **Normalization:** `"a\r\nb"` and `"a\nb"` hash identically; `"a \nb"` and `"a\nb"` identically;
  `{"k": None}` and `{}` identically.
- **Over-normalization guards (negative tests):** `"Hello"` vs `"hello"` differ; `"a  b"` vs `"a b"`
  differ.
- **`verify`:** correct hash → fresh; altered payload → not fresh; stored-column drift → integrity
  failure flagged distinctly from a stale click.
- **`hash_version`:** a row stamped `hash_version=1` verifies under a v2 code path via the v1 rules.
- **End-to-end (in `test_slack_interact.py`):** post card → mutate payload directly in the fake
  session *without* touching `payload_hash` → click → ephemeral rejection, `record_decision` **not**
  called, integrity event written. **This is the AC #1 demo test.**

### 5.8 Done when
- [ ] A click on a card whose payload changed is rejected, and nothing executes.
- [ ] A click on a card whose `config_fingerprint` changed is rejected.
- [ ] Rejection is ephemeral, names the reason, and a refreshed card is posted.
- [ ] Comparison uses `hmac.compare_digest`.
- [ ] `update_payload` is the only code path that writes `payload_hash` after INSERT (grep-verified).
- [ ] Stored-column drift raises a distinct integrity alert into `#blackink-qa`.
- [ ] AC #1 demo test passes in CI.

---

## 6. Subtask 5 — Dev 2 ↔ Dev 3 resume-command contract (FROZEN)

**STATUS: AGREED — Dev 3 may proceed.** Signed off by the project lead; Dev 2 builds against this
interface. If Dev 2 hits a problem implementing it, the contract is revised then rather than blocking
Dev 3 now. Sprint doc, Hard dependencies: *"Dev 2 and Dev 3 must agree on the resume-command payload
format before either builds their half."* — satisfied.

### 6.1 Ownership split

| Side | Owner | Deliverable |
|---|---|---|
| Persistence: `execution_halts` table, Redis keys, TTL removal, queue-worker enforcement | **Dev 2** | `src/services/halt.py` |
| Slack surface: slash commands, halt/resume cards, authorization, `resume_nonce` verification | **Dev 3** | `src/api/slack_router.py` |

### 6.2 The interface Dev 3 calls (Dev 2 implements)

```python
# src/services/halt.py — DEV 2 OWNS THIS FILE.

HaltScope = Literal["global", "client", "campaign"]

@dataclass(frozen=True)
class Halt:
    halt_id: str            # UUID
    scope: HaltScope
    scope_id: str | None    # None for global; client_id; campaign_id
    reason: str
    halted_by: str          # "slack:U123456"
    halted_at: datetime
    resume_nonce: str       # 32 bytes hex, secrets.token_hex(32), unique per halt row
    status: Literal["ACTIVE", "RESUMED"]
    resumed_by: str | None
    resumed_at: datetime | None

def engage(*, scope: HaltScope, scope_id: str | None, reason: str,
           halted_by: str) -> Halt: ...
    # Writes Postgres row (status ACTIVE) AND Redis key, in that order.
    # NO TTL on either. Idempotent: an existing ACTIVE halt for the same
    # (scope, scope_id) is returned unchanged rather than duplicated.

def release(*, halt_id: str, resume_nonce: str, resumed_by: str) -> ReleaseResult: ...
    # Verifies hmac.compare_digest(resume_nonce, row.resume_nonce) AND status == 'ACTIVE'.
    # UPDATE ... WHERE halt_id = :id AND status = 'ACTIVE' — 0 rows ⇒ NOT_ACTIVE.
    # Deletes the Redis key only after the Postgres UPDATE commits.
    # Returns: RELEASED | NOT_ACTIVE | BAD_NONCE

def is_halted(*, client_id: str | None = None,
              campaign_id: str | None = None) -> bool: ...
    # True if a global halt OR a matching client/campaign halt is ACTIVE.
    # FAILS CLOSED: if Redis is unreachable, returns True (halted).
    # Rationale carried from FA/relay/guards.py:113-125 — deferring work is
    # always safe; resuming sends because the halt store was unreachable is not.

def active_halts() -> list[Halt]: ...
```

### 6.3 Invariants — non-negotiable, both sides

1. **No TTL, ever, on any halt key or row.** A halt ends only via `release()`. This is the entire
   point of Week 0 §3.0.2-B. Do not port `FA/relay/config.py:43`, its use at
   `FA/admin_router.py:2288`, or the "Auto-clears on expiry" text at `:2291`.
2. **Postgres is the source of truth; Redis is the fast path.** On worker start, and on Redis
   reconnect, Redis is rehydrated from Postgres `status='ACTIVE'` rows — a Redis flush must not
   resurrect sending. This is what makes AC #3's "across server restarts" true.
3. **`is_halted` fails closed** (§6.2).
4. **A resume is bound to one specific halt row** via `resume_nonce`. A resume for halt #1 can never
   clear halt #2 — the concrete stale-resume scenario: operator halts, resumes, halts again; the
   first resume card is still sitting in Slack and must not clear the second halt.
5. **Authorization is fail-closed** — `approver_authorized()` (§4.3). Empty allowlist ⇒ nobody.

### 6.4 Slack surface (Dev 3 builds)

**Slash commands** — both route to `POST /api/slack/command`, dispatched on the `command` field.
Slash-command bodies are plain form-encoded, **not** wrapped in a `payload` field like interactive
callbacks (`FA/admin_router.py:2261-2262`); the invoking user is a top-level `user_id`, not
`payload.user.id` (`FA/admin_router.py:2277-2281`).

```
/blackink-halt  <global|client|campaign> [scope_id] [reason...]
/blackink-halt  status
/blackink-resume  <halt_id>      ← fallback path; normally the operator clicks the card button
```

**On halt:** call `halt.engage(...)`, then post to `#blackink-command` (blueprint §3.0.3 assigns
global pause/resume controls to that channel) a card carrying a **Resume** button whose value is:

```json
{"kind": "halt_resume", "halt_id": "<uuid>", "resume_nonce": "<64 hex>"}
```

The nonce rides inside a Slack-HMAC-signed body, so it cannot be forged in transit. Handled in
`/interact` under `action_id = "halt_resume"` — registered in **`CONTROL_ACTIONS`, not
`WORK_ORDER_ACTIONS`** (§4.3). It deliberately bypasses steps 6–9 of the click flow: a halt has no
work order, no payload hash, and — for a global halt — no `client_id` at all, so the work-order path
would fail on every one of those steps. Its authorization call is therefore
`approver_authorized(user_id)` with no `client_id` argument.

**Deliberate simplification, stated rather than hidden:** no separate `BLACKINK_HALT_RESUME_SECRET`
HMAC over the resume payload. Slack's request signature already proves origin, and `resume_nonce`
already proves freshness against a specific halt row; a second HMAC would add a secret to rotate
without covering a threat the first two miss. If the threat model later includes a compromised Slack
workspace, the upgrade path is to require two distinct approvers on release (`release()` gains a
second-approver argument) — which is a stronger control than another HMAC, and a cheaper change than
unwinding one.

### 6.5 Redis keys (Dev 2 implements, Dev 3 must not bypass)

```
Tenant/campaign scope — via src/core/tenant_redis.py (client_id is a required positional arg):
    tset(client_id, "halt", "client")             → "1"      # no TTL
    tset(client_id, "halt", "campaign", campaign_id) → "1"   # no TTL

Global scope — genuinely not tenant-scoped, so tenant_redis cannot express it:
    global_key("halt", "global")                  → "1"      # no TTL
```

⚠ **Requires a `config/redis_keys.py` change and a CI-lint allowlist entry.** CLAUDE.md mandates all
tenant-scoped Redis go through `tenant_redis.py`, and a grep-lint flags raw `redis_client` primitives
used elsewhere. A global halt has no `client_id` by definition — inventing a sentinel
(`client_id="__global__"`) would be worse: it fabricates a fake tenant inside a namespace whose whole
guarantee is that every key belongs to a real one. So: add `global_key(*parts) -> str` to
`config/redis_keys.py` returning `f"global:{...}"`, and add `src/services/halt.py` to the lint
allowlist with a comment pointing here. **Dev 1 owns `redis_keys.py`** — raise this with them rather
than editing around it.

### 6.6 Tests — `tests/test_slack_halt_command.py` [NEW], joint

Dev 3's half (no live DB, `halt.py` mocked):
- Non-approver `/blackink-halt` → "Not authorized", `engage` not called.
- Empty allowlist → "Not authorized" (fail-closed regression test).
- Bad signature → 401 before any halt call.
- `/blackink-halt global` → `engage(scope="global", scope_id=None, ...)` exactly once.
- Resume click with the correct nonce → `release()` once, card updated.
- Resume click with a tampered nonce → `BAD_NONCE`, ephemeral rejection, no release.
- **Stale-resume:** halt A resumed, halt B engaged, then A's old card clicked → `NOT_ACTIVE`, halt B
  still active. (Invariant 4 — the scenario the nonce exists for.)

Dev 2's half — restart persistence and Redis-flush rehydration — is a live-Postgres test in Dev 2's
suite. Dev 3 should not duplicate it.

### 6.7 Done when
- [ ] Contract in §6.2 agreed by both devs before either starts (signature-level agreement, in writing).
- [ ] `grep -rn "ttl" src/services/halt.py src/api/slack_router.py` finds nothing halt-related.
- [ ] Non-approver cannot halt or resume; empty allowlist authorizes nobody.
- [ ] Tampered nonce is rejected; stale resume cannot clear a newer halt.
- [ ] `redis_keys.global_key` change agreed with Dev 1 and the lint allowlist updated.

---

## 7. Subtask 3 — Slack app manifest & channel setup

### 7.1 The six channels

Names are **verbatim from blueprint §3.0.3:156-161** and the sprint doc. Note that two carry no
`blackink-` prefix — that is correct, not a typo, and must not be "tidied" when creating them.

| Channel | Purpose (blueprint §3.0.3) | Settings field |
|---|---|---|
| `#blackink-command` | Executive overview, macro pipeline queries, active tenant statuses, global pause/resume | `blackink_command_slack_channel` |
| `#blackink-setter` | 1-screen context cards for high-intent owner leads | `blackink_setter_slack_channel` |
| `#sales-replies` | Inbound reply stream, email/SMS, automated intent tags | `sales_replies_slack_channel` |
| `#dial-tasks` | Daily phone queue: company background, response latencies, direct lines | `dial_tasks_slack_channel` |
| `#blackink-qa` | Health logs, API heartbeats, domain reputation deltas, cross-tenant leakage alerts | `blackink_qa_slack_channel` *(already exists in `config/settings.py`)* |
| `#blackink-economics` | CAC, channel unit economics, wallet caps | `blackink_economics_slack_channel` |

⚠ **DIVERGENCE:** blueprint §3.0.3's ASCII diagram (`:142-153`) shows five channel boxes; its own
prose (`:156-161`) lists six. The prose and sprint doc agree on six. Build six.

**`@Blackink Router`** (blueprint §3.0.3:145) is not a second app — it is this same app in its
dispatcher role (§5.1:1364 calls it "@Blackink Central Router"). One Slack app, invited to all six
channels.

### 7.2 `config/slack_channels.py` [NEW]

```python
CHANNEL_REGISTRY: dict[str, ChannelSpec] = {
    "command":   ChannelSpec(name="#blackink-command",   settings_field="blackink_command_slack_channel",   interactive=True),
    "setter":    ChannelSpec(name="#blackink-setter",    settings_field="blackink_setter_slack_channel",    interactive=True),
    "replies":   ChannelSpec(name="#sales-replies",      settings_field="sales_replies_slack_channel",      interactive=True),
    "dial":      ChannelSpec(name="#dial-tasks",         settings_field="dial_tasks_slack_channel",         interactive=True),
    "qa":        ChannelSpec(name="#blackink-qa",        settings_field="blackink_qa_slack_channel",        interactive=False),
    "economics": ChannelSpec(name="#blackink-economics", settings_field="blackink_economics_slack_channel", interactive=False),
}

def resolve_channel_id(channel_key: str) -> str | None: ...
    # Registry → settings field → channel ID. None when unconfigured
    # (callers no-op rather than raise — FA/relay/slack_post.py:44-56 pattern).
```
Store channel **IDs** (`C01234ABCDE`), not names: names can be renamed out from under the config, and
resolving names at runtime would need the `channels:read` scope for no benefit.

### 7.3 Slack app manifest

**Bot token scopes — minimum that works:**
| Scope | Why |
|---|---|
| `chat:write` | Post and update cards. |
| `chat:write.public` | Post to a channel the bot has not been explicitly invited to — a safety net so a missed invite degrades to "posts anyway" rather than a silent drop. |
| `commands` | `/blackink-halt`, `/blackink-resume`. |

Deliberately **not** requested: `channels:read` (IDs are configured, §7.2), `channels:history`,
`users:read`, `files:write`. Nothing in Dev 3's five items reads history or user profiles; requesting
a scope now that goes unused is a permission the workspace grants for nothing. `views.open` (modals)
needs no scope beyond a valid bot token.

**Interactivity & Shortcuts:** enabled, Request URL = `https://<host>/api/slack/interact` — **one URL,
dispatching on `action_id`**. FA's docstring at `admin_router.py:1643-1650` records what happens
otherwise: three features each registered their own Request URL, so at most one was ever reachable
from Slack. Do not add a second Request URL for any reason.

**Slash commands:**
| Command | Request URL | Hint |
|---|---|---|
| `/blackink-halt` | `https://<host>/api/slack/command` | `<global\|client\|campaign> [id] [reason]` |
| `/blackink-resume` | `https://<host>/api/slack/command` | `<halt_id>` |

**Event Subscriptions:** not enabled in Week 0. Inbound reply ingestion (`#sales-replies` content) is
Week 1's Interim Reply Bridge, fed by email/SMS webhooks, not Slack events. Dev 3 creates the channel;
it does not subscribe to Slack events to fill it.

### 7.4 `config/settings.py` [MODIFY]

```python
# ── Slack: channels (IDs, not names — see config/slack_channels.py) ──
blackink_command_slack_channel:   Optional[str] = Field(default=None, env="BLACKINK_COMMAND_SLACK_CHANNEL")
blackink_setter_slack_channel:    Optional[str] = Field(default=None, env="BLACKINK_SETTER_SLACK_CHANNEL")
sales_replies_slack_channel:      Optional[str] = Field(default=None, env="SALES_REPLIES_SLACK_CHANNEL")
dial_tasks_slack_channel:         Optional[str] = Field(default=None, env="DIAL_TASKS_SLACK_CHANNEL")
blackink_economics_slack_channel: Optional[str] = Field(default=None, env="BLACKINK_ECONOMICS_SLACK_CHANNEL")
# blackink_qa_slack_channel already present.

# ── Slack: authorization (FAIL CLOSED — empty means nobody) ──
blackink_global_approvers: tuple[str, ...] = Field(default=(), env="BLACKINK_GLOBAL_APPROVERS")
```
Per-tenant approvers live in `clients` (a Dev 1 table) and are read through
`src/services/client_config.py`, not added to settings — one env var per tenant does not scale past
the founding client.

### 7.5 Done when
- [ ] All six channels exist with exactly the names in §7.1 (both un-prefixed names intact).
- [ ] One Slack app, member of all six.
- [ ] Interactivity Request URL set, exactly one, pointing at `/api/slack/interact`.
- [ ] Both slash commands registered.
- [ ] Only the three scopes in §7.3 requested.
- [ ] A card posts to each interactive channel and a click round-trips (AC #1 live check).

---

## 8. Build order & dependency gates

### 8.1 Can start immediately (no blockers)
1. `src/services/slack/auth.py` — pure fork, needs only `settings`, which exists.
2. `src/services/slack/payload_hash.py` + `tests/test_payload_hash.py` — pure functions, no DB, no
   Slack. **Start here:** it is the AC #1 core and has zero dependencies.
3. `tests/helpers/slack_signing.py`.
4. Slack app creation, channel creation, manifest, scopes (§7) — org/config work, parallel to code.
5. `requirements.txt` + `slack_sdk`.

### 8.2 Gated on Dev 1
| Dev 3 work | Needs from Dev 1 | Why |
|---|---|---|
| `apply_agent_work_orders.py` | `clients` table (landed: `migrations/apply_clients.py`) | FK target. **Already satisfied.** |
| `agent_work_orders` RLS | `tenant_policies.py` + `apply_rls_policies.py` (landed) | Registry entry is a Dev 3 edit to a Dev 1-owned file — coordinate, don't fork the file. |
| `events` writes | `events` table (landed: `apply_events.py`, `models.py:409`) | **Already satisfied** — Dev 3 is not blocked on Dev 1's item 8. |
| `redis_keys.global_key()` | Dev 1 owns `config/redis_keys.py` | §6.5. Small change, ask early; only blocks the halt surface, nothing else. |
| Per-tenant approvers | `clients` columns / `client_config.py` | Only blocks *tenant-scoped* approver widening. Ship global-allowlist-only first; both empty still means nobody authorized, so the fail-closed guarantee holds regardless. |

### 8.3 Gated on Dev 2
| Dev 3 work | Needs |
|---|---|
| `/blackink-halt`, `/blackink-resume`, `halt_resume` button | `src/services/halt.py` implementing §6.2 |

**Unblock without waiting:** agree §6.2's signatures with Dev 2 on day 1, then build the Slack surface
against a mocked `halt` module. Dev 3's tests (§6.6) mock it anyway, so the entire Slack half can be
finished and tested before Dev 2's implementation lands. The signature agreement is the real gate —
it costs an hour and unblocks both devs.

### 8.4 Suggested sequence

Ordered by dependency, not by calendar — nothing here is dropped or deferred; the numbering is the
order in which each piece stops blocking the next.

```
A  freeze §6.2 with Dev 2 (first action of the sprint — unblocks step E for both devs)
   raise redis_keys.global_key() with Dev 1 (§6.5)
B  payload_hash.py + test_payload_hash.py          — zero deps, and it is the AC #1 core
   slack/auth.py + tests/helpers/slack_signing.py
   Slack app + 6 channels + manifest + scopes      — org work, parallel to all code
C  apply_agent_work_orders.py + models.py + tenant_policies.py + RLS re-run
D  work_orders/__init__.py (enqueue → get → record_decision → update_payload)
   work_orders/dispatchers.py (noop)
   work_orders/__main__.py (--health, --seed, --sweep)   — the AC #1 demo driver
E  slack_router.py: /interact (block_actions + view_submission + view_closed)
   post.py · hash enforcement in the click path · modal open + submit (§4.3a)
   /command halt + resume surface against a mocked halt.py
F  AC #1 end-to-end demo test · AC #3 stale-resume test
   live workspace round-trip · CLAUDE.md updates · Reuse Ledger inputs to Dev 4
```

Note on the sprint's own calendar: Week 0 is dated Aug 31 – Sept 2 and overlaps Week 1 from Sept 1
(the sprint doc flags this in its header). That is a scheduling fact, not a licence to cut scope —
every item above is required by the sprint doc or the blueprint. If time pressure appears, the thing
to escalate is the schedule, not the deliverables: steps A–C and E carry AC #1 and AC #3, and F is
where they are actually proven.

### 8.5 CLAUDE.md updates required on completion
Per its Self-Maintenance section: new `src/services/slack/` package · new `config/slack_channels.py` ·
new external integration (`slack_sdk`) · `models.py` schema change · new tenant-bearing table (with
the matching `tenant_policies.py` entry) · new migration in the ordered command list.

---

## 9. Risks & open questions

| # | Item | Detail | Proposed default |
|---|---|---|---|
| 1 | ✅ **DECIDED — `Revise` behavior** | Sprint doc and blueprint §3.0.3 both list `Revise` as a button; neither says what it does. Options: open a modal to edit the payload (then rotation §5.6 applies), or reject-with-note back to the drafting agent. | Ship `Revise` as a modal that captures a free-text revision note, records `SKIPPED` with the note in `execution_receipt`, and posts to `#blackink-setter`. It does **not** edit the payload in Week 0 — in-Slack payload editing needs a full re-hash-and-repost round trip and is Week 1 Campaign Agent work. **Decided: build exactly this.** |
| 2 | ✅ **DECIDED — `Snooze` duration** | No default anywhere in the source docs. | Static select on the card: 1h / 4h / tomorrow 9am tenant-local. `until` stored in `due_at`. **Decided: keep the `America/New_York` fallback (all Week 0/1 target metros are FL) with a `ponytail:` comment naming the ceiling; do NOT add a tenant-timezone column in Week 0.** |
| 3 | **`EntityLeaseManager` is not Week 0** | Verified by grep: no "lease"/"lock" anywhere in blueprint §3.0 (Week 0, lines 99–199); all mentions are §5 (lines 1375, 1404, 1734–1742), the full-build architecture. Not one of item 1's three named things, and no AC needs it. | Deferred to Week 1 (first real caller: Campaign Agent). Defect analysis preserved in the §3.3a appendix so it is not re-discovered. |
| 4 | **Blueprint `agent_work_orders.client_id` FK is wrong** | `REFERENCES companies(company_id)` — see §3.3. | Repo convention wins (`clients(client_id)`, VARCHAR(40)). Same class of blueprint-SQL error CLAUDE.md already records for `company_id`. Flag to the client for the blueprint's own correction. |
| 5 | **Channel-count contradiction inside the blueprint** | §3.0.3 diagram shows 5, prose lists 6, §5.1 lists 8 (incl. per-tenant). | Six, per sprint doc + §3.0.3 prose. Registry accommodates the per-tenant two later without a code change. |
| 6 | ✅ **DECIDED — Band 2 on Week 0 cards** | Blueprint §5.1 says every new action class starts at Band 1 (observe/report, zero autonomous action), but a Band 1 order by definition has no Approve button to click. | Week 0 cards are `BAND_2_ONE_TAP` — a human clicking Approve *is* the authorization, which is exactly Band 2. Band 1 orders are posted as notices with no action block. **Decided: Week 0 cards are `BAND_2_ONE_TAP`.** |
| 7 | **No tenant approver storage yet** | Per-tenant allowlists need a `clients` column. | Global allowlist only in Week 0; both-empty still means nobody. Raise the column with Dev 1 for Week 1. |
| 8 | **`chat:write.public` judgement call** | Lets the bot post to channels it was never invited to — convenient, slightly broader than strictly needed. | Keep it: a missed channel invite otherwise silently drops health and QA alerts, which is a worse failure than the extra scope. Drop it if the client objects on least-privilege grounds. |
| 9 | **Slack 3-second ACK vs. hash recompute + DB read** | Steps 1–10 in §4.3 run inline. Comfortable at Week 0 volume; a slow DB could push past Slack's 3s timeout and trigger a retry (which the `status='QUEUED'` guard makes harmless). | Inline for now. If p99 approaches 3s, move steps 9–10 into a `BackgroundTasks` job and ACK immediately — the pattern already exists at `FA/admin_router.py:1926-1978`. `ponytail:` comment at the handler. |
