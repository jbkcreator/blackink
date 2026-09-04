# Choose the email sender

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

**Decision: direct SMTP per warmed Google Workspace / Outlook mailbox.**
See "Answered by `Week1_Tasks_Dev_Split_v2.md`" below. Residual procurement
question (do the seats exist, who pays, credential mechanism) is CLIENT-ASKS
**A14**, not a design blocker.

## Update from ticket 04 (closed)

**Instantly is eliminated.** It cannot set `In-Reply-To`, cannot attach
files, and has no general send endpoint — any one of which is fatal. Its
tracking webhooks (`email_opened` / `email_link_clicked`) are the only
salvageable piece, and only on the ~$97/mo Hypergrowth plan.

So this ticket is no longer "Instantly or not". It is: **who mints the
`Message-ID`?**

- **Direct SMTP per warmed mailbox** — the only option giving outright
  header control plus a literal "send from these 6 mailboxes we selected".
  Cost: self-hosting open/click tracking (a redirect service and a pixel
  endpoint we build and operate), plus 40 sets of mailbox credentials to
  store.
- **Postmark / SES / Resend / SendGrid** — header control, attachments, and
  tracking webhooks out of the box. Caveats found: **SES overwrites
  `Message-ID`** with its own (workable — read it back and store that),
  and **Postmark's custom `Message-ID` preservation reportedly needs
  `X-PM-KeepID: true`**, which appears in support guidance but not the API
  reference and is **unverified** — verify before relying on it.

The sharp question for whichever provider: does "send as
`mbx04@growth-blackink.com`" mean the provider is a relay for a domain we
own and warm (fine — reputation stays ours, per §7.3's no-pooling rule), or
does it mean sending from the provider's shared pool (not fine)? Confirm
per-candidate, because §7.3's tenant-isolated reputation requirement is the
whole point of the 3-domain/6-mailbox-per-client architecture.

Also now decided by 04: `build_instantly_sequence` in `outbound_templates.py`
is almost certainly dead code, and `instantly_enabled` may still be worth
keeping purely for the warmup analytics the service already reads.

## Re-scoped 2026-09-03 — separate *hosting* from *dispatch*

Two corrections from the Source of Truth.

**First: domain warmup and DNS are not in Dev 3's scope**, and not a code
deliverable at all. The blueprint has "DNS access **delegated** to configure
SPF, DKIM and DMARC across 20 dedicated domains (40 mailboxes), initializing
domain warmup schedules **ahead of campaign launch**" (p6); the domains are
"**pre-purchased**" (p16); and onboarding allocates a cluster "**from the
pre-warmed pool**" (p22). `CLAUDE.md` says the same — manual runbook, not
code, per Forced Action's ADR 0011.

What *is* in scope: refuse to dispatch from a mailbox whose `warmup_status`
is incomplete or whose domain is `quarantine_state`-flagged (see ticket 09,
which suspects the dispatcher does not check this today); the sentinel's
quarantine-and-rotate; and the preflight verification gate
(`check_domains_spf_dkim_dmarc_valid`, GATE-08). Warmup itself is a client
dependency — CLIENT-ASKS A3.

**Second, and this reframes the ticket: the blueprint used Instantly as the
mailbox and warmup *platform*, not as the sender.**

- onboarding "assigns pre-warmed sending domains, **Instantly
  sub-workspaces**…" (p30)
- Vera's margin model line-items "**Instantly mailbox costs**" (p33)
- the sentinel: "**Instantly quarantines** degraded domain; routes traffic to
  warmed backup domain pool" (p39)

The repo agrees. `InstantlyService` is `get_warmup_analytics`,
`get_daily_analytics`, `list_accounts` — telemetry about mailboxes, never a
sender. Ticket 04 eliminated Instantly **as the sender**, and that stands.
It did not eliminate Instantly as the **hosting/warmup platform**, and those
are separable.

### The question is now: who hosts the mailboxes, and can we get SMTP to them?

The blueprint names the mailboxes explicitly: dispatch happens "from warmed
**Google Workspace/Outlook inboxes** across dedicated domains with verified
SPF/DKIM/DMARC" (p11), and onboarding allocates "6 **Google Workspace**
mailboxes" (p22).

If they are real Workspace/M365 seats, then:

- **SMTP is available**, so `Message-ID` and `In-Reply-To` are fully
  controllable and Touch 3's threading DoD is satisfiable.
- **Replies land in real monitored inboxes**, which cold outbound requires —
  Touch 1 asks for a reply and 3.1.3 must catch it. An API-only sender
  identity has nowhere for a reply to go.
- Instantly (or equivalent) can still own warmup scheduling and telemetry
  over those same mailboxes, which is what `InstantlyService` already reads.

If instead the mailboxes are locked inside an Instantly sub-workspace with no
SMTP access, we inherit Instantly's header limitations and **Touch 3's
threading DoD becomes unmeetable** — at which point either the mailboxes move
or the DoD is renegotiated.

**This is the deciding fact and it is a client question** — CLIENT-ASKS A14.

## ✅ Answered by `Week1_Tasks_Dev_Split_v2.md` (2026-09-03)

Shared Definition of Done, item 1:

> "confirmed email dispatches from **warmed Google Workspace / Outlook
> inboxes** across dedicated domains with verified SPF/DKIM/DMARC"

**Real mailbox seats, not an API sender identity.** So:

- **SMTP is available** → `Message-ID` and `In-Reply-To` are fully
  controllable → Touch 3's threading DoD is satisfiable.
- **Attachments work** — and v2 requires two (Owner Visibility Score PDF on
  Touch 1, Fee-Stack one-pager on Touch 3), which alone rules out every
  API-only path Instantly offered.
- **Replies land in real monitored inboxes**, which is what 3.1.3 needs.
- **Our dispatcher picks the mailbox**, satisfying §7.1 and the 30–50/day cap.

**Decision: direct SMTP per warmed mailbox.** Instantly is out as sender
(ticket 04) and its remaining role is warmup/mailbox telemetry only, which
`InstantlyService` already covers.

**What this leaves to build:**

- **Credential storage for up to 34 mailboxes.** Google Workspace / M365 seats
  → app passwords or OAuth2 SMTP. Too many for inline settings; needs a
  secrets-manager reference or credential columns on `mailboxes`. Still routed
  through `get_settings()` (`CLAUDE.md`).
- **Open/click tracking is ours.** No provider gives it with raw SMTP — build
  a redirect service and a pixel endpoint, hosted on neither the brand domain
  (`getblackink.com`, protected) nor a cold-outreach domain. Emit
  `email_opened` / `email_clicked`.
- **Confirm which mailboxes exist.** v2 assumes warmed Workspace/Outlook
  seats; `SENDING-DOMAINS.md` shows the domain inventory is 3 short of the
  20-domain plan and none are in the database. Provisioning remains
  CLIENT-ASKS **A3**/**A14**.

### Costs to confirm once hosting is known

- **Credentials for up to 34 mailboxes** (17 sendable domains × 2). Too many
  for inline settings — likely a secrets-manager reference or credential
  columns on `mailboxes`. Note `get_settings()` is still the only permitted
  accessor (`CLAUDE.md`).
- **Open/click tracking is ours to build** if dispatch is raw SMTP — a
  redirect service and a pixel endpoint, hosted somewhere that is neither the
  brand domain (`getblackink.com`, protected) nor a cold-outreach domain.
- **Attachment requirement may vanish.** Ticket 11 has not decided whether
  the Owner Visibility Score report is a PDF attachment or a hosted page. If
  hosted, the attachment constraint drops. Prefer not to lock the sender
  before 11 settles — though if the reply-mailbox argument holds, SMTP wins
  regardless.

### Clarification worth recording

§1.6's "do not build Gmail or Microsoft Graph **mailbox** access" is about
**client** mailboxes in the Respond product — the point being that a general
mailbox grant would expose tenant PII (§10.1: "that inbox carries tenant IDs,
bank details, fair-housing accommodation requests"). It does **not** forbid
operating our own outbound sending mailboxes. Easy to conflate later; written
down here deliberately.

## Question

Nothing in the repo transmits mail. Decide what does.

The constraints Task 3.1.1 places on the sender are unusually tight:

- Send from a **specific** warmed mailbox chosen by our own dispatcher (§7.1)
  — the sender must accept "use this mailbox", not pick one itself.
- Expose Touch 1's **`Message-ID`** for persistence, and accept
  **`In-Reply-To`** on Touch 3 (§5.2).
- Carry a **per-recipient PDF attachment** and inline GIF (§4.1).
- **Open and click tracking** surfaced as webhooks we can map to
  `email_opened` / `email_clicked` (§27.1).
- Respect a **30–50/mailbox/day** cap that *we* enforce (§7.2), which means
  the sender must not silently re-pace or re-route.
- **Tenant isolation** — no sending identity or reputation pooled across
  clients (§7.3).

Given ticket 04's findings, choose: Instantly campaign API, direct SMTP per
warmed mailbox, a transactional provider, or a hybrid (e.g. Instantly for
warmup state, SMTP for dispatch).

Settle explicitly:

- If Instantly cannot set `In-Reply-To`, is Touch 3's threading DoD
  renegotiated, or is Instantly dropped as the sender?
- If direct SMTP: where do 40 mailboxes' credentials live, and how does that
  square with `get_settings()` and the no-`os.environ` rule? Does
  `mailboxes` gain credential columns, or a secrets-manager reference?
- Does `build_instantly_sequence` in `outbound_templates.py` survive this
  decision, or become dead code?
- What happens to `instantly_enabled` (currently defaults False)?

## Why it matters

This is the widest-blast-radius decision in the map. It determines the
sequence state model (whether we store Message-IDs at all), the tracking
design, the credential story, and whether the 24h cap is enforced by us or
by a vendor.
