# Campaign Agent Architecture

> Design decisions captured from architecture session, 2026-09-02.

---

## 1. System Overview

Campaign Agent is the orchestrator that runs the full outbound lifecycle for a single prospect. It owns the top-level state, coordinates subagents, and manages the two async boundaries (IMAP reply wait, Slack approval wait). It does not draft copy and does not send — those belong to Cora and Relay respectively.

```
┌─────────────────────────────────────────────────────────────────┐
│                       CAMPAIGN AGENT                            │
│                   (LangGraph, Postgres checkpoint)              │
│                                                                 │
│  CampaignState                                                  │
│  ├── company_id, campaign_id, work_order_id                     │
│  ├── stage: enum                                                │
│  ├── ghost_result, latency_sec, loss_est                        │
│  ├── pdf_url, video_id, landing_url, gif_url                    │
│  └── error                                                      │
│                                                                 │
│  GHOST      WAIT_     PDF + SENDSPARK   CORA    WAIT_   RELAY   │
│  SHOPPER -> REPLY  -> + GIF (parallel)->DRAFT ->APPROVE->SEND   │
└─────────────────────────────────────────────────────────────────┘
       ▲                    ▲                         ▲
       │                    │                         │
  campaign:         imap:reply_received          slack:approvals
  work_orders          (Redis Stream)            (webhook resume)
  (Redis Stream)
```

---

## 2. Agent Roles

| Agent | Type | What it does |
|---|---|---|
| **Campaign Agent** | Orchestrator | Owns lifecycle, holds state, coordinates everything |
| **Cora** | Worker / consumer | Drafts copy when told to, posts to Slack for approval |
| **Relay** | Worker / consumer | Sends approved drafts, enforces rate limits and halt checks |
| **Ghost Shopper** | Subagent (in-process) | Crawls PM website, submits inquiry, records timestamp |
| **PDF Generator** | Subagent (in-process) | Compiles branded loss report PDF |
| **Sendspark** | Subagent (in-process) | Generates personalised video landing page |
| **GIF Generator** | Subagent (in-process) | Produces animated thumbnail for Email 1 |

---

## 3. Execution Flow

```
campaign:work_orders (Redis Stream)
        │
        ▼
┌───────────────────┐
│  1. GHOST SHOPPER │  in-process subagent
│     subagent      │  returns { submitted_at, result }
└────────┬──────────┘
         │  SUBMITTED
         ▼
┌───────────────────┐
│  2. WAIT_REPLY    │  Campaign Agent CHECKPOINTS here (Postgres)
│     (suspend)     │  Process can die — state survives
└────────┬──────────┘
         │  imap:reply_received event resumes agent
         │  { latency_sec, loss_est } written to CampaignState
         ▼
┌─────────────────────────────────────────┐
│  3. PARALLEL EXECUTION                  │
│                                         │
│  PDF Generator ──────────────────────►  │
│                                         │  all in-process,
│  Sendspark ──────────────────────────►  │  parallel await
│       └── returns { video_id,           │
│                     landing_url }       │
│                                         │
│  (GIF waits for Sendspark landing_url)  │
│  GIF Generator ──────────────────────►  │
│       └── uses landing_url as input     │
└────────┬────────────────────────────────┘
         │  { pdf_url, video_id, landing_url, gif_url }
         ▼
┌───────────────────┐
│  4. CORA DRAFT    │  publishes to cora:drafts (Redis Stream)
│                   │  Cora worker picks up, drafts Email 1–5
│                   │  using all proof assets, posts to Slack
└────────┬──────────┘
         │
         ▼
┌───────────────────┐
│  5. WAIT_APPROVE  │  Campaign Agent CHECKPOINTS again
│     (suspend)     │  Slack approval webhook resumes
└────────┬──────────┘
         │  approved draft
         ▼
┌───────────────────┐
│  6. RELAY SEND    │  publishes to relay:sends (Redis Stream)
│                   │  Relay worker picks up, checks halt_service,
│                   │  enforces rate limits, dispatches via Instantly
└───────────────────┘
```

---

## 4. State Design

### CampaignState (top-level, LangGraph)

```python
class CampaignState(TypedDict):
    # Identity
    company_id:     str
    campaign_id:    str
    work_order_id:  str

    # Lifecycle
    stage:          CampaignStage   # enum drives LangGraph routing

    # Ghost Shopper output
    ghost_result:   Literal["SUBMITTED", "FORM_NOT_FOUND", "ERROR"] | None
    submitted_at:   int | None      # ms timestamp

    # IMAP output
    latency_sec:    int | None
    loss_est:       int | None      # dollars, from revenue loss formula

    # Proof asset outputs
    pdf_url:        str | None
    video_id:       str | None
    landing_url:    str | None
    gif_url:        str | None

    # Cora output
    draft_ids:      list[str] | None

    # Error
    error:          str | None
```

### SubagentState (local, never persisted to LangGraph)

Each subagent maintains its own state internally for the duration of its invocation. Campaign Agent only sees the return value.

Example — Ghost Shopper:
```python
@dataclass
class CrawlState:
    company_id:      str
    start_url:       str
    visited:         set[str]
    candidate_queue: list[ScoredURL]
    depth:           int
    max_depth:       int            # default 3
    max_llm_calls:   int            # hard budget cap, default 6
    llm_calls_used:  int
    submitted_at:    int | None
    result:          Literal["PENDING", "SUBMITTED", "FORM_NOT_FOUND", "ERROR"]
```

---

## 5. Queue Architecture

### Decision: Redis Streams for all queues

RabbitMQ is not needed at current scale. Redis Streams with consumer groups, PEL, and dead-letter handling (pattern established in `cora:drafts`) covers all requirements.

### Queue Map

```
campaign:work_orders      external trigger → Campaign Agent
                          produced by: Prospecting Agent / manual
                          consumed by: Campaign Agent execution layer

imap:reply_received       IMAP Listener → Campaign Agent resume
                          produced by: IMAP background service
                          consumed by: Campaign Agent (resumes checkpointed state)

cora:drafts               Campaign Agent → Cora worker
                          produced by: Campaign Agent (step 4)
                          consumed by: Cora worker pool
                          (already implemented — src/agents/cora/queue.py)

relay:sends               Campaign Agent → Relay worker
                          produced by: Campaign Agent (step 6)
                          consumed by: Relay worker pool
```

### Queue Boundary Rule

| Use a queue when | Use in-process when |
|---|---|
| Two sides run in different processes | Both sides in same execution context |
| Unbounded async wait (hours) | Operation completes in seconds |
| Durability required (process can die) | Return value is sufficient |

Ghost Shopper, PDF Generator, Sendspark, GIF Generator are all **in-process** — no queues between them and Campaign Agent.

---

## 6. Ghost Shopper Subagent Design

### Architecture: Stateful Crawl Loop (plain Python, no LangGraph)

```
Input: company_id + website_url
         │
         ▼
┌─────────────────────────────────┐
│         LOOP ENTRY              │
│  pop next URL from candidate    │
│  queue (start: homepage)        │
└────────────┬────────────────────┘
             │
             ▼
     ┌───────────────┐
     │  FETCH &      │  Playwright fetches page
     │  EXTRACT      │  extracts <a> hrefs + <form> elements
     │               │  merges new hrefs into candidate queue
     └──────┬────────┘
            │
            ▼
     ┌───────────────┐
     │  FORM         │  LLM node (Claude Sonnet)
     │  VALIDATOR    │  input: compressed form HTML (fields, labels, names)
     │               │  output: { valid: bool, reason: str }
     └──────┬────────┘
            │
       ┌────┴──────┐
     VALID      NOT VALID
       │              │
       ▼              ▼
┌──────────┐   ┌──────────────────┐
│  FILL &  │   │  QUEUE RANKER    │  LLM node (Claude Sonnet)
│  SUBMIT  │   │                  │  scores + re-ranks candidate queue
│(Playwright)  │  (conditional)   │  returns ranked list
└────┬─────┘   └──────┬───────────┘
     │                │
     ▼                ▼
┌──────────┐   queue empty OR depth/budget limit hit?
│  CONFIRM │        │
│  CHECK   │       YES → FORM_NOT_FOUND → log → exit
│(rule-based)
└────┬─────┘
     │
┌────┴────┐
SUCCESS  FAILED
  │          │
  ▼          └──► re-queue, continue loop
log + exit
```

### Ghost Shopper Nodes

| # | Node | Type | Responsibility |
|---|---|---|---|
| 1 | FETCH & EXTRACT | Playwright | Fetch HTML, extract `<a>` + `<form>`, merge hrefs into state |
| 2 | FORM VALIDATOR | LLM (Sonnet) | Is this a valid owner inquiry form? |
| 3 | FILL & SUBMIT | Playwright | Fill fixed template, submit form |
| 4 | CONFIRM CHECK | Rule-based | Detect confirmation text or URL change post-submit |
| 5 | QUEUE RANKER | LLM (Sonnet) | Score and re-rank candidate URLs |
| 6 | LOOP CONTROLLER | Rule-based | Check termination conditions, advance state |

**Optional future nodes:** CAPTCHA_HANDLER, IFRAME_EXPANDER, JS_FORM_TRIGGER

### Termination Conditions

- `result == SUBMITTED` — form confirmed submitted
- `candidate_queue` empty — no more URLs to try
- `depth >= max_depth` (default 3) — too deep, give up
- `llm_calls_used >= max_llm_calls` (default 6) — budget exhausted

### LLM Usage

| Node | Calls per run | Purpose |
|---|---|---|
| Form Validator | 1 per page with forms | Classify form validity |
| Queue Ranker | 1 per page without valid form | Rank candidate URLs |
| **Total** | **1–2 typical, 6 max** | |

This is 5–10× cheaper than a browser-use library approach (which makes one LLM call per action step, typically 8–12 per run).

### Fixed Submission Template

```
Name:     Jordan Mitchell
Email:    audit-bot@audit-blackink.com
Phone:    (813) 555-0192
Address:  4821 Harborview Dr, Tampa FL 33611
Message:  "Hi, I own a single-family rental property and I'm evaluating
           property management companies in the area. Could someone reach
           out to discuss your services and fees? Best time to call is
           weekday afternoons."
```

Email is fixed — the IMAP listener matches replies by sender domain against this inbox.

---

## 7. Parallel Execution — Steps 3–5

```
latency_sec + loss_est available
        │
        ├──────────────────────┬──────────────────────┐
        ▼                      ▼                       │
┌──────────────┐      ┌──────────────────┐             │
│ PDF Generator│      │    Sendspark     │             │
│              │      │                  │             │
│ formula:     │      │ API call with    │             │
│ Monthly Leads│      │ company_name,    │             │
│ × decay(lat) │      │ speed, loss      │             │
│ × fee × ret  │      │                  │             │
│              │      │ returns:         │             │
│ returns:     │      │ video_id         │             │
│ pdf_url      │      │ landing_url ─────┼─────────────┘
└──────────────┘      └──────────────────┘             │
                                                       ▼
                                             ┌──────────────────┐
                                             │  GIF Generator   │
                                             │                  │
                                             │ screenshot of    │
                                             │ prospect website │
                                             │ + speed overlay  │
                                             │ + landing_url    │
                                             │                  │
                                             │ returns: gif_url │
                                             └──────────────────┘

PDF and Sendspark: parallel (no dependency)
GIF: sequential after Sendspark (needs landing_url)
```

---

## 8. Checkpointing Strategy

LangGraph checkpointer backend: **Postgres** (same cluster, survives Redis evictions).

Two suspend points:

| Stage | Trigger to resume |
|---|---|
| `WAIT_REPLY` | `imap:reply_received` event published to Redis Stream |
| `WAIT_APPROVE` | Slack interactive webhook fires approval callback |

Both resume by rehydrating `CampaignState` from Postgres checkpoint and re-entering the graph at the correct node.

---

## 9. Halt Integration

Relay checks `halt_service` before every send:

```
relay:sends consumer picks up approved draft
        │
        ▼
is_halted(client_id)?
        │
   ┌────┴────┐
  YES        NO
   │          │
   │          ▼
   │    enforce rate limits
   │    dispatch via Instantly API
   │    log touch_sent to events
   │
   └──► leave message unacked in PEL
        claim_stale() will skip it (halt-aware, already fixed)
        resumes automatically when halt is lifted
```

Cora can continue drafting during a halt. Only Relay is gated — drafts queue up, sends are held.

---

## 10. Build Order

```
1. CampaignState schema + CampaignStage enum
2. Campaign Agent LangGraph graph (nodes stubbed, routing wired)
3. Postgres checkpointer wired to LangGraph
4. campaign:work_orders Redis Stream consumer
5. WAIT_REPLY suspend + imap:reply_received resume signal
6. Ghost Shopper subagent (plain Python crawl loop)
7. PDF Generator subagent
8. Sendspark subagent
9. GIF Generator subagent
10. WAIT_APPROVE suspend + Slack webhook resume
11. relay:sends Redis Stream + Relay worker
```

Ghost Shopper is step 6. The Campaign Agent contract (steps 1–5) must exist before Ghost Shopper is built so its return interface is defined upfront.
