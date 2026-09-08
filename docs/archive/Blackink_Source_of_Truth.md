# Blackink — Source of Truth

Compiled: 3 September 2026. Scope: all four supplied documents, including their complete extracted text, source code, tables, examples, legacy requirements, and open questions.

This is a consolidated requirements reference, not evidence that the platform has been implemented or that any reported production state remains current. No application repository was supplied. “All code” here means all code printed in the supplied documents; it does not mean a recovered Blackink application codebase.

## Reading order and authority

1. **Comments:** `blackink comments.txt` controls explicit corrections and decisions.
2. **Blueprint:** `Project Blackink - Complete Implementation Blueprint (Full).pdf` supplies the detailed implementation baseline where the comments do not override it.
3. **Business end to end:** `Blackink_Business_Need_End_to_End_Flow.docx` supplies the business explanation and examples, subject to the first two sources.
4. **Unified specification:** `Blackink - DoorEngine Unified System Specification.pdf` supplies additional detail. Because the user specified the first three priorities but did not place this file, this compilation treats it as supplementary rather than allowing it to override them silently.

Within the comments, explicit statements that a rule is “now” changed, “stale,” “reverses,” or “do not build” take precedence over the older passages they identify. Physical position alone does not determine recency: the opening correction summary overrides several later pasted sections. Unresolved contradictions remain identified below.

**Normative reading rule:** apply the decisions in Part 1 to the baseline in Part 2. The full-source sections preserve superseded text for completeness; they do not reactivate cancelled features, prices, channels, or deadlines. No code excerpt in the source archive should be run as an approved migration without reconciling it against these decisions.

## Navigation

- [Part 1 — Comments: controlling decisions](#part-1--comments-controlling-decisions)
- [Part 2 — Blueprint: implementation baseline](#part-2--blueprint-implementation-baseline)
- [Part 3 — Business end-to-end operating flow](#part-3--business-end-to-end-operating-flow)
- [Part 4 — Unified specification: supplementary requirements](#part-4--unified-specification-supplementary-requirements)
- [Part 5 — Conflicts, missing inputs, and code cautions](#part-5--conflicts-missing-inputs-and-code-cautions)
- [Part 6 — Source and code coverage](#part-6--source-and-code-coverage)
- [Source A — Complete comments](#source-a)
- [Source B — Complete implementation blueprint, 39 pages](#source-b)
- [Source C — Complete business document](#source-c)
- [Source D — Complete unified system specification, 11 pages](#source-d)

## Part 1 — Comments: controlling decisions

### 1.1 Business objective, delivery boundary, and geography

Blackink sells B2B to residential property-management firms. Its value is more managed doors, fewer lost doors, and more revenue from the existing portfolio. The paying customer is the management firm; the property owner is the prospect or existing owner served for that firm. Blackink must not become a tenant-response service.

The September build is judged by a working chain, not a completed phase list. Marketing starts mid-September, with an 11 September demonstration/marketing gate retained in the source discussions; sales begin at the end of September. By **30 September 2026**, a previously unknown firm must be able to see its rank, receive its report, talk to Blackink, sign, pay, complete onboarding, and have its first owner inquiry answered without manual technical work on Blackink's side. Human sales and response approvals remain where explicitly required; “no manual step” must not be read as permission to bypass them.

The **county is the unit everywhere: data, ranking, contract, and seat**. There is no metro layer. National coverage means many counties, not a different geographic model. Postal addresses and property-level filters remain useful data, but must not silently recreate metro/ZIP-zone commercial exclusivity.

| Product or capability | Controlling reach |
| --- | --- |
| Owner Visibility Score / free report | National from day one; public observable data |
| Respond | National from day one; forwarding alias and client contacts |
| Retention Guard and dead book | National from day one as the stated product ambition; actual data coverage must be shown honestly |
| Attended appointments | Cleared counties only; contracted polygon and state counsel clearance |

County launch priority: **Hillsborough and Pinellas**, reusing existing Forced Action records. Next: **Orange, Duval, Polk, Pasco, Lee, Brevard, Volusia, Seminole**. Miami-Dade, Broward, and Palm Beach are deliberately not first. No new Forced Action sources are authorized for this month merely because additional Blackink counties are listed.

### 1.2 Separate Blackink product, shared implementation foundation

Blackink has a dedicated repository, dedicated development and production environments, and a **separate UI**. It is a fork of Forced Action, not a greenfield implementation. Reuse its frontend components, layouts, and patterns and the shared **Cora, Vera, Hunter, Relay** foundations. Maintain a module-by-module reuse ledger. Fix shared defects once in the shared layer.

No Figma or brand guideline is required for internal implementation. Use functional requirements where precedent is absent, and receive feedback on the running product. Daily approval screens deserve particular attention. The public rank report is explicitly an exception to minimal design effort: it is a major credibility surface and needs careful presentation.

Provision production before writing the outcome table. No production PII in development or staging. Nightly backups begin on day one, with an actual tested restore before the first paying client.

### 1.3 Forced Action stabilization dependencies

These are source-reported conditions, not newly verified live findings:

- Forced Action enters stabilization: stop new features, scraper sources, engines, and surfaces this month.
- Keep its scheduler, all 41 source freshness checks, existing subscriber billing, security patches, and backups healthy.
- The comments report freshness dropping from 39 to **23 of 41**, with 18 stale sources, and development/production both on revision `1442596c2209`.
- Investigate in order: scheduler survival after deployment; run one stale source manually; inspect shared proxy, session, or authentication dependencies. The proposed shared cause is a diagnostic hypothesis, not a proven root cause.
- Add a post-deploy scheduler-alive check; alert on freshness decline; distinguish **ran with no results**, **ran and failed**, and **did not run**. Two scheduled sources writing nothing are also identified for investigation.
- Resolve **7 active subscribers with NULL `plan_price`**. Reconciliation was reported as 0/0 and drift $0 for four days; those numbers are contextual observations, not proof that NULL pricing is acceptable.

### 1.4 Week-zero corrections and proof requirements

| Item | Controlling requirement | Required proof |
| --- | --- | --- |
| W0-1 Cora | Pause draft generation and requeue at 50 unreviewed drafts **or** any unreviewed draft older than 24 hours | No new drafts/requeue while paused; one Slack notification per incident; clean resume without loss or duplicates; aged-draft trigger works at low volume |
| W0-2 Vera | Proper revenue/billing fix within the stated 8 hours; `UNKNOWN` / `ABSTAIN` first-class in Shared Agent Core | Missing data cannot become a financial zero; separately time-box a two-hour sweep and return severity-ranked findings |
| W0-3 Hunter | Provision dedicated worker, first determining migration versus second box | Avoid divergent duplicate resolvers; if migrated, prove nightly sweep on the new instance; existing workload is described as 807K entities |
| W0-4 Infrastructure | Dedicated repo/dev/prod with reuse, not independent reimplementation | Production ready before outcome schema; no production PII in lower environments; nightly backups and tested restore |

Relay's persistent halt from the blueprint remains applicable: a TTL or restart must never re-arm a paused campaign. An operational lease TTL is not permission to expire a safety halt.

### 1.5 Data intake, domain, and provider decisions

Publish the upstream interface and validation rules without waiting for Akrash's first file. Minimum supplied schema:

`firm_name · website_domain · county · door_count_estimate · door_count_source · two contacts (name, role, email, phone, source) · enriched_at · submitted_by · validation_status · reject_reason_code`

Map this contract explicitly to blueprint storage names rather than silently losing fields. Return rejected rows with reason codes; never silently drop them.

`getblackink.com` is the protected brand/identity domain. Blackink does **not** own `blackink.io`. Other domains are cold-outreach domains. If video is later hosted, use `watch.getblackink.com`. September includes a video trigger hook and disabled/configured provider row only, **not the video generation/delivery flow**. No Sendspark account was supplied.

No RentCast or CoreLogic contract exists. The Rent Analysis Bot is deferred to **Q1** (the comments do not specify the year). Define only an inexpensive adapter interface if appropriate, keep provider disabled, and do not start a trial on the client's behalf.

Calendar integration uses **Google Calendar and Microsoft Graph**; **GoHighLevel** is the fallback when the client has no connectable calendar. No Calendly product subscription/API is required. Meetings belong on the client's event, with attendance evidence for both parties and a `proof_ref`.

Telnyx is the selected carrier for any retained/future telephony integration; remove conflicting Twilio assumptions. **No AI voice this year**, outbound or callback. **No SMS this year** is stated in W1-4. Earlier transactional/demo SMS plans are preserved in the archive but are not a September dependency. If a future demo messaging flow is re-enabled, the comments require a second number, separate campaign, and prospect-initiated text, never the production Customer Care / Account Notification campaign.

### 1.6 Respond: forwarding, reply identity, and service promise

Do not build Gmail or Microsoft Graph **mailbox** access. Calendar OAuth remains. Clients forward their owner-inquiry address to a Blackink address; more than one source address may forward to the alias. No general mailbox read permission is held. Voicemail-to-email can feed the same pipeline without adding an AI voice or transcription service.

The opening correction and Part 10 replace Part 5.1's inbox OAuth design. Part 14 adds the more specific sending requirement: send through a **delegated subdomain**, set **Reply-To to the client's own address**, and **BCC the client on every send**. The comments call for a DNS setup step at onboarding. Do not promise that outbound replies appear natively in the client's Sent folder. Build thread history from the first forwarded contact; whole-inbox coverage and preexisting history are not available.

The exact receiving alias scheme, collision policy, and how subsequent replies return to the system are not fully specified in the attachment; see Part 5. Bulk dead-book traffic must remain separate from the client's primary mailbox and its reputation, using authenticated delegated sending infrastructure.

September response promise: **within 30 minutes during business hours; next business morning after hours**. Start with human approval for every send. A template class may earn the tighter five-minute promise after **50 clean approvals**, when the behavior can be proved. Do not inherit the older sub-60-second, five-minute/24×7, or day-one autonomous wording.

Lead never discusses management fees, lease terms, rent advice, or screening criteria. Free text requiring those judgments escalates to the client. Tenant, vendor-pitch, and spam messages route to the client untouched; Lead never replies to a tenant. Approved templates do not grant blanket autonomy to novel content.

### 1.7 Lead template catalog

The three complete scripts are preserved verbatim in [Source A](#source-a); do not rewrite them as if new approved copy. Their parameters include `{first_name}`, `{client_firm}`, `{client_owner_name}`, `{slot_1}`, `{slot_2}`, and `{signoff}`.

| # | Case | Required behavior |
| --- | --- | --- |
| 1 | General enquiry | Acknowledge; ask door count and existing management; offer two times; no fees, rent estimates, or property-value opinion |
| 2 | Fees question | Escalate to client owner; offer a fifteen-minute meeting and flag thread; provide no fee figure |
| 3 | Unhappy with current manager | Ask property count and notice period; capture owner's words; do not comment on the competing firm |
| 4 | New investor / just bought | Ask unit count and when tenanting is needed |
| 5 | Out-of-state owner | Acknowledge remote ownership; retain the qualifier |
| 6 | Several properties / portfolio | Flag principal appointment type and offer a longer slot; this is routing, not permission to restore legacy principal pricing |
| 7 | Vacancy | Book meeting; no claims about marketing the unit or tenant criteria |
| 8 | Requests a phone call | Offer times; never initiate an agent outbound call |
| 9 | Outside service area | Politely decline, do not book, log ICP mismatch, never count as billable appointment |
| 10 | Tenant, vendor pitch, spam | Forward/reroute untouched; no Lead reply |

The remaining seven scripts are behavioral specifications only; no complete approved wording is supplied for them.

### 1.8 Public score and report

The name is **Owner Visibility Score**, replacing Owner Response Score. It measures public observables, not secretly tested response performance. **Do not build the ghost shopper or submit pretext inquiries.** Listing age and days vacant are excluded from v1. Portfolio size is displayed separately, not scored.

| Category | Signal | Maximum points |
| --- | --- | ---: |
| Owner conversion readiness | Separate owner-addressed page | 14 |
| Owner conversion readiness | Working owner contact form, phone, and email | 10 |
| Owner conversion readiness | Mobile, HTTPS, load under 3 seconds | 6 |
| Market visibility | Review count against county median | 16 |
| Market visibility | Review recency: under 60 days full; over 12 months zero | 12 |
| Public reputation | Review response rate | 18 |
| Public reputation | Average rating | 8 |
| Accessibility | Published after-hours contact route | 8 |
| Accessibility | Median review response lag | 4 |
| Credibility | Active broker licence and tenure | 4 |
| **Total** | | **100** |

Sources: business profiles, the firm's website, and state licence rolls. Display data coverage with the score, e.g. “76/100, 94% data coverage.” Lead with the three lowest-scoring observations and named comparisons, then score, then county rank. Publish **top 25 per county only**, never a bottom list. Below the data floor, show “insufficient data” and no rank.

Cache and refresh monthly; the alert job diffs against last month. The comments describe approximately 12,000 Florida firms and a rough low-hundreds monthly business-profile data budget, but explicitly require pricing to be confirmed at build time. These are source assumptions, not verified procurement facts. National rollout cannot assume Florida licences or complete national feeds without coverage evidence.

Loss-report default assumptions: **8% management fee, 30-month average owner tenure, approximately $100 per door/month**. Store per-client configuration and show assumptions on the report. Label estimates as estimates. Replace defaults with client actuals without deployment. Do not reuse an unobserved ghost-shopper latency as though it were measured.

### 1.9 Commercial decisions and catalog status

Latest explicitly corrected prices:

| Offer | Current source-stated price | Notes |
| --- | ---: | --- |
| Respond | $249/month | Not explicitly changed in latest correction summary |
| Respond + Retention Guard bundle | $599/month | Replaces $649 |
| Retention Guard add-on | $399/month | Replaces older $400/$349 variants |
| Retention Guard standalone | $499/month | Replaces $497 |
| First paid appointment | $49 | Replaces $97; earlier list limits to once per customer |
| Standard attended appointment | $99 flat | Any door count; replaces all door-band pricing |
| Growth OS founding | $897/month | Earlier comments: first 25; not explicitly repriced |
| Growth OS standard | $997/month | Not explicitly repriced |
| Dead-book appointment | $75 | Earlier comments retain it; relationship to new universal $99 requires confirmation |
| Extra engine | $97/month | Earlier comments list it |
| Extra office | $79/month | Earlier comments list it |
| Dead-book engine | $99/month | Separate from the earlier $75 per appointment fee |
| Additional county | $397/month | Earlier comments list it; county service must be eligible |

**Do not treat this 13-line reconciliation table as an approved 13-product Stripe manifest.** The latest comments explicitly say **twelve products, not fourteen**, but do not supply the twelve-row replacement. Replacing two old appointment bands with one flat price leaves thirteen from the earlier fourteen-row list. Which additional product is removed/merged remains unresolved.

Also, $249 + $399 = **$648**, not $599. Preserve all three source-stated prices and flag the required bundle/upgrade rule; do not silently change a price to force arithmetic consistency. The old pre-ticked $400 checkout bump and $649 total are superseded amounts, not a safe checkout configuration.

County seat products are **out of September**: retain disabled rows, do not create active Stripe seat SKUs. Growth OS, appointments, and county seats remain human-sold; the comments call for revisiting self-serve after the first 20 attended appointments, in November. Human-sold does not make disabled county seats launchable.

All commercial terms belong in configuration/entitlement rows, never agent prompts or hardcoded pricing branches. Existing legacy registry rows are retained in the complete blueprint; their existence is not authorization to activate them.

### 1.10 Self-serve signup and Launch

Only **Respond and the bundle** are self-serve. Required connective flow: claim page → Stripe Checkout with order bump → click-to-accept agreement and durable acceptance record → guided owner-file upload with column mapping → calendar connect and forwarding/sending setup → preflight → launch.

Per signup, store:

`agreement_version · accepted_at · accepted_by_email · ip · hash of the exact text shown on screen`

The accepted agreement must be retrievable for that client. Do not substitute a generic agreement URL or a hash of different text. Agreement text is still owed by the client in the supplied comments.

Appointment contracts additionally require an initialled ICP/Exhibit A and contracted polygon. Owner-file upload is a real blocking gate. Repeated uploads must respect the suppression union rather than discard owners from earlier uploads without a defined authorized removal process.

Abandonment is a real state: paid-but-calendar-blocked, paid-but-file-missing, and other incomplete states cannot disappear into silence. Nudge at **24 hours, 72 hours, and 7 days**. No second month is billed while activation is blocked on Blackink. Distinguish client-caused from Blackink-caused blocks; the exact treatment of client-caused indefinite abandonment is not fully supplied.

Real offboarding must revoke access, stop inbound forwarding/service handling, and delete owner files on a stated schedule. The exact deletion schedule and remote forwarding-removal proof are missing inputs, not grounds to invent a retention period.

### 1.11 Exhibit A: qualified owner meeting

A billable attendee must meet all supplied conditions:

1. Own or have legal management authority over at least one residential rental in the contracted county.
2. Hold the property as an investment, not a primary residence or owner-occupied home listed for sale.
3. Not already have a management agreement with the client.
4. Have no more than 60 days remaining on any agreement with another firm, or be terminable at will.
5. State before scheduling that they are considering appointing/changing manager within the next 90 days; record their own words.
6. Match the client's accepted property selections: single family, duplex–fourplex, condo, townhouse, small multifamily 5–20.
7. Not be commercial, raw land, a mobile home on leased land, or exclusively short-term rental.

Also capture free-text client exclusions, which are never billed, and minimum door count, default 1. This adds specificity to the blueprint's four-rule verification bar; flat appointment pricing does not remove ownership, territory, intent, duration, or ICP evidence.

### 1.12 Nine-link acceptance contract

The link labels and tests below consolidate comments §5.3, Part 14, and the explicit corrections. Numbering is retained even where the introductory narrative describes the chain differently.

| Link | Done means |
| --- | --- |
| 1 — See their rank | Named real firm's report generated from a cold database; at least five scored signals with sources; assumptions displayed; two independent readers find ranking defensible; use the new visibility methodology |
| 2 — Reach them | Campaign to 100 verified addresses with bounce under 3%; deliberately noncompliant draft hard-blocked; mid-campaign suppression prevents any later send |
| 3 — Talk to Blackink | Reply to a real campaign reaches a human, is recorded against the originating firm, and is answered within an explicitly configured SLA |
| 4 — Sign | Complete acceptance record including exact-text hash is produced and retrievable by client |
| 5 — Pay | Real card and ACH mandate both charge; payment updates entitlement; ACH return pauses entitlement; failed charge alerts Slack |
| 6 — Onboard | Required agreement/Exhibit A, polygon, mandate, calendar, and owner file are processed without manual technical steps; missing owner file blocks advancement; product-specific gate mapping must preserve self-serve boundaries |
| 7 — Never poach owners | Known owner email uploaded into suppression gets no campaign email; second upload respects union |
| 8 — First inquiry | Real forwarded owner inquiry receives correct reply within 30 business-hours minutes, or next business morning; demonstrate configured delegated sender, Reply-To, BCC, and thread handling; management-fee question escalates |
| 9 — Show value/proof | Charge creates exactly one email with appointment ID, commercial band/rate reference, parcel reference, proof link; dispute button writes row and pauses the line item; use flat-price classification rather than reactivating old bands |

### 1.13 MAX and learning scope

Buy/build the **outcome schema, columns, entity key, and interfaces read by learning loops in September**. Price loop logic separately and hold it for a **December** decision when sufficient outcomes exist. If genuinely inseparable, explain that rather than assuming the whole loop implementation is authorized. Preserve raw events immutably and apply **versioned corrections**; append-only history must not make false facts impossible to correct.

## Part 2 — Blueprint: implementation baseline

All details in the 39-page blueprint remain accessible in [Source B](#source-b). The following is the implementation map, with the comments' overrides applied. Unmentioned details are preserved in the source archive, not silently dropped.

### 2.1 Core invariants and data contracts

- Generic events and outcomes are built first and serve audits, messages, disputes, offer triggers, and future learning.
- Engines are **configuration row + execution adapter**, carrying routing, templates, sequence, county scope, tier, and holdout settings.
- Money, compliance, suppression, consent, and settlement gates are deterministic; LLMs may draft and classify within bounds but cannot decide financial/legal policy.
- Isolate database records, queues, Redis/cache keys, vector memory, credentials, and sending reputation by `client_id`.
- Preserve target company identity, normalized domain, website, estimated doors, PMS metadata, two role-tagged contacts, verified email status, phone type, LinkedIn URL, source/enrichment timestamps, provider, suppression flags, and outbound touch history.
- Replace metro-dependent fields/algorithms with county contracts. Preserve old `market_metro` schema text as historical, not current authority.
- Remove the old readiness prerequisite requiring a ghost-shopper score or video ID. Both underlying production features are held; otherwise the old gate would block all legitimate September campaigns.
- Staging must have explicit validation status and rejection reasons; owner-book suppression must work across clients and sending domains at dispatch time, including updates during a running campaign.

The source `company_id` contract conflicts internally: it describes a SHA-256 domain-derived identifier but SQL defines generated UUIDs. Resolve type/identity mapping explicitly. A full SHA-256 digest is not a UUID.

### 2.2 Platform stabilization, sending, and compliance

Reuse audit classifications are direct port, refactor, or pattern-only adoption. Port draft formats, execution harnesses, suppression patterns, and Slack interactions; refactor normalization, tenant sending adapters, and report builders. Record evidence and tests in the reuse ledger.

Slack action cards bind the exact payload, recipient, and configuration to SHA-256. Reject stale approvals after any change. Commands include Approve, Revise, Reject, Snooze, Skip, Mark Done, and persistent pause/resume. Source channel map: `#blackink-command`, `#blackink-setter`, `#sales-replies`, `#dial-tasks`, `#blackink-qa`, `#blackink-economics`, `#client-{name}-growth`, `#client-{name}-launch`. A channel inventory is not a named approval owner or fallback roster.

The blueprint's planning allocation is 20 domains / 40 mailboxes: five domains / ten mailboxes for Blackink and fifteen domains / thirty mailboxes for five client pools of three domains / six mailboxes. Treat those as planned allocation, not verified inventory. Respond's simpler delegated sending setup must not be confused with the bulk cold-outreach pool.

Baseline pacing is 30–50 cold emails per mailbox/day. Quarantine when bounce exceeds 3% or complaints exceed 0.08% in a rolling 48-hour window, with alerts and governed warmed-reserve handling. Maintain global suppression across every domain, even if inventory expands to 30 domains as contemplated in comments.

Keep DNC/contact eligibility, non-poach, quiet-hours, rate/cost caps, and persistent halt checks deterministic. Retained historical channel rules include no SMS 9 PM–8 AM recipient local time and cold-SMS blocking, but the comments' stronger no-SMS-this-year rule controls launch. A click, video view, or positive email is not to be used to re-enable the cancelled SMS flow.

10DLC landing-page requirements: `getblackink.com`; **HEU AI LLC, 971 US Highway 202N Ste N, Branchburg NJ 08876**; working contact email; resolving Privacy and Terms; consent disclosure beside mobile collection; plain-language Customer Care / Account Notification description. Submit minimum eligible material promptly; it is not a September SMS/billing dependency. Regulatory assertions in source documents are preserved as project requirements, not independently verified legal advice.

### 2.3 Marketing, reply routing, and booking

The older Day 0, Days 1–2, Day 4, Day 7, Day 10 sequence remains a configurable starting cadence: initial proof email, human dial task, fee-stack follow-up, manual LinkedIn follow-up, final county/public-observation angle, then cooling. Replace ghost-shopper claims, live video, metro ranks, and active seat scarcity with permitted public evidence and actual enabled offers. LinkedIn uses manual deep links/copy, not automated scraping/browser outreach.

Reply routing must stop cold sequences when appropriate and keep complete firm/contact context. The blueprint names **ten** classes: `HOT_LEAD`, `QUESTION`, `OBJECTION`, `LATER`, `NURTURE`, `UNSUBSCRIBE`, `COMPLAINT`, `LEGAL_GRIEF`, `WHALE_OWNER`, `PARTNER`. Keep opt-out and legal/complaint safety handling deterministic. Do not let generic knowledge-base confidence override the owner-topic blocklist.

Legacy routing timers are 15 minutes for unclaimed-hot-lead ping, 60 minutes executive escalation, 240 minutes backup closer. These are internal escalation values, not replacements for the controlling 30-minute Respond promise. A 90% classifier/KB threshold is a confidence threshold, not authorization to send.

Bookings must write events, use the client calendar, carry real attendance evidence, support reschedule/no-show recovery, and provide contextual confirmation email. The source's 30-minute pre-demo email can remain an email trigger, but it must not advertise the held rent bot/video. Preserve post-meeting capture: attendance, PMS, doors, objections, next action. Demo/sandbox data must be clearly distinguished from real client proof.

### 2.4 Launch state machine and preflight

The blueprint supplies these 15 setup states; adapt their old mechanisms to current decisions rather than deleting their underlying business purpose:

| State | Baseline purpose | Current interpretation |
| --- | --- | --- |
| 1 | Agreement signed | Click acceptance or human contract with exact retained acceptance evidence |
| 2 | Payment setup | Plan-appropriate Checkout/payment and ACH/card records; not universal zero-upfront billing |
| 3 | Contacts | Broker/owner and operations contacts |
| 4 | PM profile | Assets, specialties, language, service/ICP configuration |
| 5 | Territory | County and contracted polygon; no metro commercial layer |
| 6 | Calendar | Google/Microsoft; GHL fallback; original baseline asks 25 slots in 60 days |
| 7 | Offer and CTA | Approved message/qualification settings; no agent-written fee promises |
| 8 | Historical intake | Current owner file for suppression is mandatory; dead-book data as applicable |
| 9 | Documents | Agreements/fee schedules and permitted audit inputs |
| 10 | Partner menu | Preserve optional/held selections without pretending unavailable services are live |
| 11 | Growth elections | Goals, fee modifications, ancillary/service interests |
| 12 | Sending/compliance | Delegated sender, domain authentication, suppression, channel controls |
| 13 | Lead routing | Forwarding alias/webhook routing, correct client mapping and reply path |
| 14 | Live-fire | Current email/SLA test, not cancelled SMS or sub-60-second promise |
| 15 | Preflight/launch | All applicable gates green; approval and activation evidence |

Every completion event cancels the relevant chase. Add abandonment/blocked states and timed nudges from the comments. Product-specific required/optional states are a remaining design input: an entry Respond customer must not be forced to buy unrelated partner services or possess 50 dead leads merely because the legacy generic preflight required them.

Baseline ten preflight gates cover Stripe setup, profile, territory, calendar slots, historical records, documents/audit, partner menu, authenticated domains, suppression, and roundtrip. Source thresholds of ≥25 open slots/60 days and ≥50 historical records are preserved, but require explicit per-product applicability.

Golden Client provisioning and written cloner/rollback runbooks remain useful. The source names `clone_tenant_environment.sh` and `rollback_tenant_provisioning.sh` but supplies no script bodies. Provision tenant identity, credentials, sending configuration, suppression, routing, and first-14-days plan. Early pilot clients may receive engineering help; the September 30 repeat must work without application code changes, including self-serve entry onboarding.

### 2.5 Revenue engines, retention, and expansion

| Engine / surface | Baseline capability | Controlling qualification |
| --- | --- | --- |
| Respond / Speed-to-Lead | Ingest owner enquiries, qualify, route, book | Forwarding + email; human approvals and current SLA |
| Revive / Win-Back | Normalize historic leads; timing memory; three-touch reactivation | Suppression and separate bulk sending; no SMS/AI calls |
| Grow / owner intelligence | Public records, multi-LLC entity resolution, portfolio aggregation, scoring | County-first; no ghost shopper; eligible data sources only |
| Retain / Churn Tripwire | Listing/deed/homestead/address/anniversary risk alerts | Distinguish alert/save opportunity from verified `door_saved` |
| Rent Gap Report | Compare actual lease rates against market; baseline $197/month | Dependency and commercial status unresolved; no unsupported rent API activation |
| Owner Report Card | Quarterly branded owner report; baseline $197/month and 60-day trigger | Preserve as blueprint capability; not explicitly in revised Stripe list |
| Close detection | Parse forwarded management-software confirmation into signed evidence | No assumption of live PMS OAuth connectors |
| Dead-book engine | Recurring activation + verified appointment fee | Reconcile flat-price/dead-book exception and catalog count |
| Second pass | After ≥90 days, route unclosed eligible lead | Transferability permission and current owner-book suppression required |
| Referral / partner | Credits, enrollment, attribution, testimonials/case-study drafts | Human agreements/financial execution; realtor delivery conflict remains flagged |
| County seats | First-refusal/exclusivity logic | Disabled for September |
| Async close | Small-owner electronic signing flow in old blueprint | Contract/negotiation autonomy conflicts; not an approved shortcut around current ICP and human sales boundaries |

Preserved blueprint specifics include First-14-Days plans (Win-Back/Respond, then Tripwire/outbound, then partner/review), retention vectors (sale listings, deeds, homestead, mailing-address changes, 60-day pre-anniversary notices), rent gap threshold ≥10%, owner/portfolio recipes, and ancillary events. They are fully available in the archive even when dependent or deferred.

Referral source figures: $250 client referral at first paid month; $500 new-county referral; free-tier appointment credit; vendor $50/door in $20/$10/$10/$10 tranches. These are legacy commercial definitions and must not silently enter the new twelve-product build.

### 2.6 Outcome verification, billing, and disputes

Preserve four-rule appointment verification: verified residential parcel in contracted polygon; both parties attended at least **12 minutes** with `proof_ref`; recorded pre-qualification before booking; initialled ICP/Exhibit A match. Unknown evidence must not become a passing value. Failure of ownership, pre-qualification, or ICP means no charge; the source distinguishes replacement treatment for legitimate attendance failure.

The blueprint says five business days for disputes, one free replacement per four billables with maximum four/month, no goodwill obligation when rolling 90-day signed-to-attended conversion is at least 15%, and penalty-free termination below 8% for two consecutive months. The unified source instead says 48 hours/immediate unverifiable credit. **At this document priority, five business days is the provisional blueprint default; final agreement must resolve and lock the applicable rule.** Do not expose incompatible promises to different surfaces.

The blueprint contains both recurring subscriptions and a universal-sounding zero-deposit/50–50 success-settlement narrative. Current self-serve paid subscriptions override “zero upfront for all clients.” Keep 50% at signature / 50% at day 60 and cancellation clawback as an **offer-specific historical design**, not the default for $99 attended appointments. Exact offer applicability remains open.

Financial actions require deterministic, idempotent entitlement and payment logic; missing inputs halt and alert. LLM transcript analysis cannot independently decide charges, refunds, or legal outcomes. Preserve evidence packets: source/outreach lineage, engagement/booking, qualification/attendance, and management-contract proof where the offer requires it. A charge receipt and dispute link must be exactly-once in the acceptance test.

### 2.7 Agent workforce and shared core

| Agent | Responsibilities retained under current scope |
| --- | --- |
| Launch | Intake, missing-item chase, provisioning, preflight, launch, offboarding |
| Prospecting — Hunter/Scout | Entity matching, county data, owner portfolios, candidate scoring, source quality |
| Campaign — Cora/Relay | Drafts, approved sequencing, pacing, lawful dispatch, public-proof hooks |
| Lead | Intent routing, approved owner templates, escalations, qualification and calendar handoff |
| Reactivation & Nurture | Future timing, old leads, no-show recovery, churn-save workflows |
| Referral | Governed partner opportunities and attribution subject to deferred scope |
| Economics & Growth — Vera | Reconciliation, quotas, cost/margin, deterministic wallet rules, reporting |
| QA / Watchdog | Freshness, heartbeat, suppression/isolation, DLQ, quarantine, evidence checks |
| Setter Copilot | Human call context, questions, objections, follow-ups, post-call capture |

Shared core includes tenant-scoped memory, typed work orders, risk/action classes, confidence, exact payload hashes, receipts, approval actor/time, leases, retries, and idempotency. An entity lease prevents contradictory concurrent work; it does not itself prove a send or charge is unique.

The source autonomy table uses Band 1 observe/report, Band 2 one-tap class approval (50 clean approvals / ≥95% approval), Band 3 bounded execution (250+ clean / <2% dispute/reversal), with fault/policy demotion. Other source passages use “Band 2” differently. Apply the comments' behavioral rule first: every early-client send reviewed; class-specific earned authority; no agent discretion over legal/financial policy. Final band naming needs reconciliation.

Capture the eight memory categories now: operational, conversation, standing client rules, counterfactual/revision reasons, win/loss, experiment registry, source quality, closing/transcript. Defer adaptive MAX loops as directed. Cross-client generalized learning must exclude PII and tenant-specific confidential data.

### 2.8 Operations, costs, security, and milestone evidence

Client Wins Dashboard shows actual attended meetings, signed agreements/doors, verified saved doors, actual versus modeled revenue, and evidence packets. Internal Control Center shows onboarding/preflight, domain health, active pipeline, and exceptions. Marketer content administration stages changes safely.

Capture enrichment, carrier, mailboxes, AI, and paid spend by client. Source economic governor bands: under $200/signed deal green; $200–$300 yellow; $300–$400 orange; above $400 red/paid-channel pause. The exact boundary equality treatment is not specified. Paid wallet baseline is $150–$300 initial risk, enforced in configuration rather than LLM judgment.

Use RBAC, encrypted secrets, daily backups/PITR, tested restore without exposing production PII in lower environments, adversarial tenant-isolation tests, suppression tests, and audit history. Retry transient failures with backoff, at most three attempts before DLQ/incident. Stale or missing required data returns `UNKNOWN`/`ABSTAIN`; cached degraded results are labelled, never falsely fresh.

Milestones retained as planning targets: Week 0 Aug 31–Sep 2; marketing demonstration Sep 11; assisted pilot Sep 16–18; portal/preflight Sep 25; full chain and two-tenant zero-code repeat Sep 30. Remove held features from old milestone tests. No attachment demonstrates that these milestones have already passed.

## Part 3 — Business end-to-end operating flow

The complete business document, including every original example, is in [Source C](#source-c). Its ghost shopping, under-one-minute response, pricing, rent bot, partner, and PMS examples are historical where overridden above.

1. Find a residential PM firm and its county, website, size band, broker/owner, and operations contact.
2. Produce an evidence-backed Owner Visibility Score/report from public observable signals, showing coverage, assumptions, and the actual county comparison.
3. Reach the firm through governed email and human follow-up. Replies reach a monitored person and remain attached to the firm record.
4. Demonstrate real enabled capabilities; do not sell held demos as live.
5. Offer Respond or bundle via self-serve, or a human-led appointment/Growth OS sale with its required contract and eligibility.
6. Capture exact agreement acceptance and authorized payment; map them to entitlements.
7. Configure county/service coverage, accepted residential assets, minimum doors, exclusions, real calendar, and owner-file suppression; upload historical leads when applicable.
8. Configure forwarding and authenticated sending, prove the roundtrip, and pass the appropriate Launch gates.
9. Answer new owner enquiries within the current SLA using approved templates and escalation rules; route tenant messages untouched.
10. Reactivate appropriate old leads using prior timing/context; recheck suppression before sending.
11. Find new eligible owners through county/property data, resolving fragmented LLCs into portfolios without confusing PM firms and property owners.
12. Triage replies and provide the human setter with context; do not answer blocked fee/lease/rent/screening questions.
13. Book on the client's calendar and preserve pre-qualification, parcel, ICP, and attendance evidence.
14. Verify the outcome; invoice only under that customer's agreed offer and applicable deterministic rules.
15. The PM firm handles negotiation/closing; signed management agreements add actual managed doors, distinct from appointments booked.
16. Issue receipt/evidence, provide dispute access, and reflect payment/return/dispute state correctly.
17. Monitor the active book for supported churn signals; alert the firm. Only verified retention outcomes count as saved doors.
18. Provide enabled owner reporting/revenue opportunities, with estimates labelled and unsupported integrations held.
19. Reconcile costs and actual value, retain audit/correction history, and repeat onboarding for the next firm without application code changes.

The original fictional example uses Tampa Premier with 350 doors, 500 annual enquiries, 1,800 dead leads, and 20–30 owners leaving annually. Its illustrative month contains 200 new prospects, 80 reactivated leads, 25 inbound enquiries, 35 conversations, 18 booked meetings, 12 qualified attended meetings, 4 signed owners, 16 new doors, 6 retention alerts, 2 retained owners, and 7 saved doors. These are **illustrations, not measured results or guarantees**. The original Michael example adds eight doors, taking 350 to 358; it is a separate example, not a competing monthly total.

## Part 4 — Unified specification: supplementary requirements

The complete 11-page text and SQL are in [Source D](#source-d). Retain its additional information while recognizing the lower assigned priority.

### 4.1 Presentation and sales artifacts

Six source demo screens:

1. **Owner Packet:** 5–10 named owners, recent 14-day distress signals, legal name/address, county portfolio doors, estimated rent, ZIP, distress tags, score, management posture, rent-advance eligibility, and exact extraction timestamp/source. Filters include location, doors, asset class, date. Export a one-page executive PDF. All data/source availability and estimates must be honest.
2. **Money Map:** configurable management fee, rent, doors per owner, close rate; monthly fee revenue, leasing/renewal capture, Month 1/4/12 accumulation, first-year cash versus ARR. Rent-advance revenue stays **PENDING** until written terms; no speculative enterprise valuation. Its 10% default is superseded by the comments' 8% loss-report default where they describe the same report assumptions.
3. **Guarantee/fairness panel:** guarantee at pricing's visual weight; no-show, active-CRM, ICP, asset, and dispute protections. The source's 48-hour credit promise conflicts with the blueprint; do not publish it as final.
4. **Empty Scoreboard:** real column names and no synthetic success rows: Owners Contacted, Owners Reached, Appointments Set, Appointments Attended, Doors Signed, Net Management Revenue Added. Date/geography filters apply.
5. **Speed-to-Lead test:** preserve the goal of proving an inbound roundtrip, but replace the old voice/SMS/<60-second demonstration with permitted email/SLA behavior.
6. **Planned network screen:** clearly labelled “PLANNED — NOT LIVE.” It is not evidence that realtor/vendor automation is operating.

Owner Brief is required before delivering an appointment to the client's calendar in this source: legal owner/entities, mailing details, parcels/addresses, portfolio, supported rent estimates, signal timeline, management posture, score/“why now,” rent-advance precheck, opener, and objections. Unavailable values must be marked unknown rather than fabricated. Offer sheet front/back contains pricing, guarantee, fairness and definitions; ZIP-zone exclusivity maps conflict with county-only comments.

### 4.2 Appointment operations and extra data requirements

Source states: `BOOKED`, `CONFIRMED_24H`, `CONFIRMED_3H`, `ATTENDED`, `DISPOSITIONED`, `RESCHEDULED`, `NO_SHOW_RECOVERY`, `REBOOKED`, `LOST`. Retain `opportunity_id` through recovery/rescheduling. Source caps reschedules at two; the third marks lost. Disposition captures SIGNED / DECIDING / NO / NOT_A_FIT, doors, reason (PRICE / TIMING / STAYING_SELF_MANAGED / WENT_ELSEWHERE / NOT_QUALIFIED), and brief accuracy YES / PARTLY / NO.

The source generated billing field requires ATTENDED plus both confirmation timestamps. That **is not a complete replacement** for the blueprint four-rule bar. Its 10DLC dependency is overridden by no-SMS launch. Resolve confirmation delivery/evidence rules before incorporating this schema into billing.

Additional public signals: out-of-area deed grantee, homestead removal, residential LLC formation, owner crossing 2 to 3+ properties, probate distribution, out-of-state mailing address. Source providers are Sunbiz, Regrid/ATTOM, county/city permit portals, Hunter/Anymail/Tracerfy. The attachments do not prove that API contracts, permission, coverage, or credentials exist. Legal tax mailing addresses support direct-mail records without an additional skip trace.

The source requests `signal_surfaced_to` instead of `introduction_made` telemetry and says vendor screens remain while realtor network is deferred. It also defers AppFolio/Buildium/Rentvine OAuth and describes CSV/webhook/forwarded-email ingestion. Its mention of “tenant data” conflicts with comments' owner-only boundary: do not ingest tenant records on that authority.

### 4.3 Superseded offers and historical launch checklist

Preserve but do not activate the unified `owner_growth_founding` offer ($397/month + $99/sit, first free, three Hillsborough seats, six-month rate lock then $749/month + $99/sit), and its separate tier table ($397/$497/$797/$2,497 base and $99/$149 sits). These conflict with county-seat deferral and the comments' current commercial list; the unified document also conflicts with itself about post-founding pricing.

Its source-reported pending items are PRs #7–#10, mocked DNC integration, missing tests for `quarantine_gate.py`, `promotion_sweep.py`, `county_allocation_reassessment.py`, DNS verification, 10DLC status, seven NULL price rows, and preflight QA. These are not independently verified current statuses. File paths and PR identifiers are references only; no corresponding repository files are attached.

## Part 5 — Conflicts, missing inputs, and code cautions

### 5.1 Decision register

| ID | Conflict / missing detail | Disposition in this source of truth |
| --- | --- | --- |
| O-01 | Bundle $599 versus $249 + $399 | Preserve all figures; require explicit upgrade/discount rule before catalog activation |
| O-02 | Twelve Stripe products versus thirteen rows after band collapse | Final twelve-item list and real Price IDs missing; no invented SKU deletion |
| O-03 | $99 any-door appointment versus $75 dead-book and older exceptions | $99 replaces door bands; confirm source-specific exceptions, first-paid semantics, legacy credits/packs |
| O-04 | Parts 15 and 16 announced but not attached | Cannot reconstruct repo credentials, alias answer, working rhythm, or commitments from absent text |
| O-05 | Per-client inbound alias/domain/collision rules | Need actual scheme, uniqueness, ownership, lifecycle and forwarding verification |
| O-06 | Reply-To client address versus persistent automated thread handling | Prove subsequent replies are forwarded back once; avoid BCC loops and duplicate ingestion |
| O-07 | Business hours, timezone, holidays, approver fallback | SLA defined at high level only; named approver/channel/fallback and schedule missing |
| O-08 | “Band 2 from launch” versus every-send review at first client | Explicit later early-client correction controls; unify labels and promotion counters |
| O-09 | 5-business-day disputes versus 48-hour credits | Blueprint priority provisionally controls; final accepted agreement must bind one policy |
| O-10 | Calendar presence versus actual attendance | `proof_ref` provider and two-party duration proof must be specified; an invite is not attendance evidence |
| O-11 | Universal zero-upfront/50–50 settlement versus paid subscriptions/attended pricing | No universal zero-upfront claim; attach settlement to explicit offer version |
| O-12 | Generic 15-state portal versus Respond-only self-serve | Define per-offer required states without erasing mandatory owner suppression, acceptance or routing |
| O-13 | National score/retention versus Florida-first operational feeds | Publish actual signal coverage; national sales claim does not imply national data completeness |
| O-14 | Score category weights given, scoring interpolation/data floor absent | Do not invent normalized formula, missing-data denominator, tie-breaks or licence treatment |
| O-15 | Blueprint realtor/referral work versus unified deferral | Preserve both; delivery decision unresolved under assigned fourth-source priority |
| O-16 | Rent Gap/Report Card present but absent revised catalog | Retain baseline and dependencies; do not fabricate enabled September SKUs |
| O-17 | Raw immutable history versus corrections and offboarding deletion | Version corrections; define PII deletion/anonymization and retention policy explicitly |
| O-18 | Client-owned forwarding setting cannot be remotely revoked automatically | Define technical shutdown plus client removal confirmation; no false claim of mailbox control |
| O-19 | 10DLC submitted/mandatory in older docs versus comments no SMS | No September SMS dependency; reported registration state still requires verification |
| O-20 | Source says “17 day-one rows” but enumerates more | Preserve all rows; do not use the headline as a reliable count |
| O-21 | Owner packet rent/advance inputs versus unavailable valuation contracts | Mark unavailable values unknown/pending; do not manufacture evidence |
| O-22 | Restore to staging versus no production PII in staging | Use an appropriate isolated production-grade recovery context or sanitized lower-environment data |
| O-23 | Client agreement text, cleared state/county scope, September 11 real firm | Required actual inputs not supplied; do not invent counsel clearance, agreement, or demo firm |
| O-24 | MAX schema/interface specification and separate estimates | Requirement supplied, complete schema and two pricing estimates not supplied |

### 5.2 Code preservation and implementation cautions

All printed SQL, JSON, and Python remains in the page-indexed archive, including its original comments and defects. The following are observations from the supplied excerpts, not claims from a running repository or tested fixes:

- `evaluate_campaign_readiness` lives under a Python-looking path in the document but is SQL/PLpgSQL. It requires held audit/video inputs and uses old field names. Preserve text; adapt deliberately before implementation.
- `company_id` UUID/SHA-256 definitions conflict; owner/PM identities and outcomes must not be merged merely because both are called “contacts.”
- The printed `EntityLeaseManager` calls `datetime.utcnow()` without importing `datetime`. PDF layout produces inconsistent indentation; the source page is the visual reference. Its get/compare/delete release is not atomic, and an agent name alone is not a unique lease token. TTL expiry can permit another worker while the original remains active. The excerpt is not proven production-safe.
- The weekly rollup joins multiple appointments/dispositions to multiple client-cost rows before aggregation, which can multiply counts and spend. Its “no denominator → 0.00” financial results need reconciliation with `UNKNOWN`/`ABSTAIN` requirements.
- `appointments.is_billable` becomes false after moving from ATTENDED to DISPOSITIONED, and does not check the full ownership/intent/ICP/duration bar. It must not be the sole billing truth.
- `idx_opportunity_dedupe` is an ordinary non-unique index, not an exactly-once financial constraint. Preserve opportunity-level billing idempotency explicitly.
- Several example tables omit explicit tenant keys, correction versions, immutable-history protections, or foreign-key relationships needed by the stated invariants. Printed DDL is not a complete application schema.
- A default `icp_criteria_passed = TRUE`, zero duration, or zero financial value must not make unknown evidence eligible. Distinguish known zero, unknown, and not-yet-evaluated.
- Billing/refund “never autonomous” wording conflicts with automatic settlement/adjudication examples. Separate authorized deterministic execution from agent discretion and bind it to the accepted offer/agreement.
- Preserve formulas as source text, including loss, confidence, and owner scoring, but do not activate deprecated ghost-shopper calculations or confuse the internal Owner Score with the new public Owner Visibility Score.

No code was rewritten, executed against a database, or silently repaired in this compilation.

## Part 6 — Source and code coverage

The archives below are an integral part of this source of truth. They preserve details too granular for the reconciled operating reference, including every obsolete alternative, table row, diagram label, repeated statement, example, code comment, and source-reported status. Repetition is intentional to avoid lossy deduplication.

PDF archives use complete `pdftotext -layout` output, split at page boundaries and fenced as text to preserve spacing and code. They are **text-layer transcriptions**, not byte-identical PDFs; graphical styling is not reproduced. Both PDFs contain selectable text and no embedded raster images. Source diagrams' text/line characters are retained as documentary evidence, not redrawn as newly authoritative architecture. Selected code pages were visually inspected against their extracted content.

The DOCX archive preserves body paragraphs, table rows/cells, line breaks, headers, footer and field instructions. Automatic Word layout/page numbering is not a new business requirement. The comments preserve all content with normalized newline encoding.

### Code and schema locator

| Source location | Printed code / technical payload |
| --- | --- |
| [Blueprint p2](#source-b-page-2) | Full `evaluate_campaign_readiness` function; source path `src/compliance/gate_evaluator.py` |
| [Blueprint p7](#source-b-page-7) | `001_core_spine.sql`: events, companies, contacts, pm_profiles and indexes |
| [Blueprint p11](#source-b-page-11) | Structured touch-event JSON example |
| [Blueprint p13](#source-b-page-13) | `002_triage_routing.sql`: inbound_messages and knowledge_base_entries |
| [Blueprint p14](#source-b-page-14) | `003_settlement_ledger.sql`: settlement_transactions |
| [Blueprint p17](#source-b-page-17) | `004_onboarding_states.sql`: client_onboarding_states and client_growth_elections |
| [Blueprint p19](#source-b-page-19) | `005_preflight_validator.sql`: client_preflight_checks |
| [Blueprint p23](#source-b-page-23) | `006_verification_disputes.sql`: appointment_outcomes and dispute_adjudications |
| [Blueprint p24](#source-b-page-24) | `007_expansion_registry.sql`: entitlement_offers and client_active_entitlements |
| [Blueprint p35](#source-b-page-35) | `008_work_orders.sql`: agent_work_orders; full printed `EntityLeaseManager` Python |
| [Unified p5](#source-d-page-5) | `009_appointment_ops.sql`: state enum, appointments, confirmation_logs, appointment_dispositions, appointment_disputes and indexes |
| [Unified p6](#source-d-page-6) | Complete weekly operational rollup SQL |

Referenced-only scripts/modules, providers, endpoints and placeholder Stripe IDs are also preserved in the archives. The attachments do not contain the implementation bodies for every referenced path. Hashes and extraction counts below document what was included, not whether the historic requirements are valid.

### Source manifest

| Source | Original file | SHA-256 of original attachment | Preserved extraction |
| --- | --- | --- | --- |
| A | blackink comments.txt | `f30be797d471c7e4ffdc669bfd832e7ebe51dbc66b824c16cc3af6e3743fddbc` | 407 extracted lines |
| B | Project Blackink - Complete Implementation Blueprint (Full).pdf | `789e5fc5bc62327d0b7efcea1d48e6300d72a53ac94a23d2b92668710c20c9c4` | 39 pages; 2044 extracted lines |
| C | Blackink_Business_Need_End_to_End_Flow.docx | `f08bce5d914f48f859dd44b64728c9dfb2948a99c5a32871d3fb05ff740d6a97` | 327 extracted lines |
| D | Blackink - DoorEngine Unified System Specification.pdf | `a3c05aeff60908ede76b21412510b895eba81428b954c45c1a028115eaa3441f` | 11 pages; 468 extracted lines |

DOCX coverage check: all 330 text nodes across document body, header and footer are present in the extraction. All four source extractions are checked against their archive blocks below. PDF form-feed page separators become page headings; other extracted page content is retained unchanged.

<a id="source-a"></a>

# Source A — blackink comments.txt

Complete source transcription. Apply the authority rules above before treating any instruction or code as current.

````text
Some of comments from client on Blackink and ForcedAction - 
Respond uses a forwarding alias, NOT mailbox OAuth. This reverses 5.1. The client sets a forwarding rule on their owner-inquiry address pointing at a Blackink address; we never hold their inbox. The OAuth mail work comes out of the build entirely. Calendar OAuth stays. Three reasons stacked up — it breaks "we never touch the tenant side," restricted Gmail scopes need app verification plus an annual third-party security assessment we can't fit in the month, and the admin-trust route kills self-serve.
Also stale: the five-minute SLA (it's 30 minutes in business hours — this includes the 11 September gate wording), appointment pricing (now $49 first, $99 flat, any door count), Retention Guard ($399 add, $599 bundle, $499 standalone), and Stripe is twelve products not fourteen.
Four new parts at the bottom, all things you'd have hit in week one:
Part 13 — the Lead agent's actual words. Ten templates, three written in full. The $249 product had no script; now it does.
Part 14 — why the alias, what it costs, and the two links that had no acceptance test. Links 3 and 4 were skipped because they need no build, and "no build" became "no proof."
Part 15 — repo and credentials, plus something inherited you should see: Forced Action freshness is 23 of 41 sources, down from 39 Friday. The county sweep behind link 1 rides on that pipeline, so it matters here. Also answers 12.2, the forwarding address scheme.
Part 16 — a working rhythm, what I owe you and how fast, and an honest line between what's still moving and what isn't.
 
Forced Action goes into stabilize mode from today. Blackink is where the build effort goes. Move anything you can across, and keep Forced Action at the lowest cost that keeps it healthy.
What stabilize means, precisely — because I don't want this read as "stop caring":
STOP: new features, new scraper sources, new engines, new surfaces. Nothing gets added to Forced Action this month.
KEEP: the scheduler running and the 41 sources fresh, billing correct for the existing subscribers, security patches, and backups. This is not maintenance-optional.
The reason it isn't optional, and it's the important part: Blackink is a fork of Forced Action and reuses Cora, Vera, Hunter and Relay. Forced Action's quality is Blackink's quality. If the shared layer rots, Blackink inherits the rot in week two. So when you fix something in the shared components, fix it in the shared layer once rather than twice — that's the whole reuse ledger working the way it's supposed to.
Which makes this morning's regression the first thing, not something to leave behind. 23 of 41 sources fresh, down from 39 on Friday. Eighteen going stale simultaneously is one shared thing breaking, not eighteen failures — and prod and dev both moved to 1442596c2209 over the weekend, so a deploy went out while it happened.
Three checks, in this order:
Is the scheduler actually running after the deploy? If cron lives in a container that got replaced or a process that doesn't auto-restart, everything stops at once and that's the entire explanation.
Run one stale source by hand. That splits "scheduler dead" from "scrapers broken" in five minutes.
Check the shared dependency — one proxy pool, one session handler, one auth token that all the county scrapers use.

Three cheap fixes worth doing while you're in there:
A deploy hook that verifies the scheduler is alive afterwards. This happened during a deploy weekend and it will happen again.
Alert on freshness dropping, not just on errors. I found this by reading a number; it should have raised a notice.
Distinguish "ran and found nothing" from "ran and failed" from "didn't run." Three states currently collapsing into one — and note that the two sources scheduled and writing nothing are silent zeros by another name. This is the same bug class as W0-2, which means you now have a live production proof case for why that fix matters.


Also still open from Friday, and it belongs in stabilize scope: the 7 active subscribers carrying NULL plan_price. Reconciliation is 0/0 and drift $0 four days running, so that's the last thing on an otherwise clean ledger.
 
One more addition to the same document — Part 9, self-serve signup, and it ships in September.
A firm has to be able to find us, buy, and be onboarded without speaking to anyone, by 30 September. This is the difference between growth capped by my calendar and growth that isn't.
The good news: most of it is already in the nine links. Stripe, the Launch state machine, the OAuth connect, the rank report and landing page — all being built anyway. The only genuinely new work is the connective tissue:
Claim page → Stripe Checkout with the order bump
Click-to-accept agreement + acceptance record 
Guided owner-file upload + column mapper 
Abandoned-onboarding states + nudge sequence 

Three things about it that matter:
1. The click-to-accept agreement is load-bearing, not a formality. Self-serve means nobody countersigns anything, so the acceptance record IS the contract. Store per signup: agreement_version · accepted_at · accepted_by_email · ip · hash of the exact text shown on screen. Without that record the appointment bar, the dispute window, the replacement cap and the ICP exhibit are all unenforceable — the first dispute becomes a refund instead of an argument. This turns link 4 from "0 hours, Josh's problem" into a small real build item. I still owe you the agreement text this week.
2. Only Respond and the bundle are self-serve. Everything above stays human. Appointments need a contracted polygon and an ICP the client initialled — letting someone buy attended appointments without a person confirming what they count as a qualified owner is how the first dispute becomes a refund. Growth OS, appointments and county seats stay human-sold until we've watched the first twenty attended appointments. Revisit in November.
3. The abandonment case will happen. Someone pays, then stalls at the OAuth screen or never uploads the owner file. That needs a real state, not silence — nudge at 24h, 72h and 7 days, and no second month billed while the account is blocked on us. Charging month two to a firm that never got switched on earns a chargeback and a bad review at the same time.

The order bump lives inside checkout — Retention Guard added for $400 more, pre-ticked, making it the $649 bundle. That's the highest-return row in the business and checkout is the only place it fires cleanly.
 
Two corrections to anything you already read:
The unit is the COUNTY. Everywhere — data, rank, contract, seat. There is no metro layer; I considered it over the weekend and rejected it. A county is a hard boundary that already matches the parcel data, and "the only firm in Hillsborough County" is enforceable in a way "the only firm in Tampa" isn't. This is one less thing to build, not one more.
No AI voice at all this year. Not outbound, not callback. Off the build, off the counsel list

On MAX: we're buying it, and I want it priced in two halves. The schema — outcome table, columns, entity key, the interfaces the loops read — cannot be back-filled and must be right in September. Buy that now. The loop logic reads a table that'll be nearly empty all month, so price it and hold it; we decide in December when it has something to learn from. Two numbers please, not one. If they genuinely can't be separated, say so and I'll buy the whole thing.
We sell to property-management firms, B2B, end to end. The whole system gets built in September. Marketing starts mid-September. We sell at the end of September. On 30 September a firm that has never heard of us must be able to see its own rank, receive a report, talk to us, sign, pay, be onboarded, and have its first inbound owner inquiry answered — with none of those steps done by hand on our side.

"Done" is that chain working end to end. It is not a phase list — a phase can be complete while the chain is still broken.

You will hit a hundred small decisions in September I will not be in the room for. The question each time: does this get a paying firm through those nine steps? If yes it is in. If no it waits.

Part 4 · Your thirteen open items — the decisions
W0-1 Cora pause threshold. 50 unreviewed drafts, plus an independent trigger on any draft older than 24 hours. Two triggers, because a count alone misses a slow leak. The defect is that the pause must stop generation, not just sends. Acceptance: queue at 50 → no new drafts and the requeue stops · one Slack notice, not one per blocked attempt · clean resume with no duplicates or lost items · an aged draft pauses generation even at low count.
W0-2 Vera silent zeros. Narrow fix now. Fix revenue and billing properly (the 8 hours budgeted) · make UNKNOWN/ABSTAIN a first-class return type in Shared Agent Core — if only one of these happens, make it this one · time-box 2 hours to sweep and hand back a severity-ranked list, nothing else fixed silently inside the 8.
W0-3 Hunter worker. Yes, provision it. One check first: Hunter/Scout already runs on its own droplet for the 807K-entity resolution — confirm migration or second box, because two resolvers with drifting data is worse than the problem. If migration, the nightly-sweep proof runs on the new instance.
W0-4 Repo and servers. Dedicated repo yes, dedicated dev and production yes. READ THIS TWICE: a separate repository is not a separate codebase. Blackink uses the Forced Action front end as its base and reuses Cora, Vera, Hunter and Relay. It is a fork with a reuse ledger, not a greenfield build. If you have read it the other way, tell me this week — that reading does not fit in one month at any headcount. Provision production before the outcome table is written · no production PII in dev or staging · nightly backup from day one and one restore actually tested before the first paying client.
W1-1 Target list / Akrash boundary. Confirmed as you described. Build the interface to a spec, not to a file — publish the schema and validation rules now, accept the first batch whenever it lands. Minimum schema: firm_name · website_domain · county · door_count_estimate · door_count_source · two contacts (name, role, email, phone, source) · enriched_at · submitted_by · validation_status · reject_reason_code. Rejected rows return with a reason code; nothing dropped silently.
W1-2 Sendspark / video domain. No account yet, and watch.blackink.io cannot be set up — we do not own blackink.io. getblackink.com is the protected brand and identity domain; the rest are cold-outreach only. Video, if hosted, goes on watch.getblackink.com. Do not build the video flow in September — build the trigger hook and leave the provider behind a config row.
W1-3 UI / frontend. No Figma, no branding guideline, and none needed. Take the Forced Action components, layouts and patterns. Where there is no precedent, design to the functional requirements — no review cycle, no mock-up round, I give feedback on the running thing. The one surface worth real care is the daily approval screens. Quote the frontend against reuse; a greenfield number is the wrong number.
W1-4 10DLC landing page. It is the rank report's front door, not a separate site, and it is already inside the build. In the order carriers check: hosted on getblackink.com · HEU AI LLC, 971 US Highway 202N Ste N, Branchburg NJ 08876, working contact email visible · Privacy and Terms that resolve · a form collecting a mobile number with the consent disclosure visible at the point of collection · a plain-language description consistent with Customer Care and Account Notification, not marketing. Ship the minimum version, submit immediately, improve while queued. Not a blocker — we send no SMS this year.
W1-5 Rent valuation API. Neither. No contract exists with either provider, so the Rent Analysis Bot cannot be built — it is already Q1 for that reason. Define the adapter interface only if close to free, leave the provider row disabled. Do not start a trial on our behalf.
W1-6 Ghost shopper.Hold. Do not build it. Every other item on your list is an engineering question; this one asks us to submit an inquiry to a real business under a pretext, at volume, then publish and sell a ranking built on it. Rank v1 runs on public observable data only — see Part 5. Methodology we can publish when a ranked firm asks how we got their number.
W1-7 Loss-report numbers. 8% management fee · 30-month average owner tenure · ~$100 per door per month. These are per-client config rows, not constants — a firm charging 10% will reject a national assumption. Every report states its inputs on the page. Straight with you: these are estimates, not observations. The first real client's actuals replace the defaults, and that must not require a deploy.
W1-8 Calendly. The premise is off and it is cheaper than expected — we need no Calendly plan for the product at all. Blackink never books into a Blackink calendar: Launch has calendar-connect as an explicit gate and the attendance bar requires both parties on the client's own event with a proof_ref. Integrate against Google Calendar and Microsoft Graph; GoHighLevel is the fallback for a client with no connectable calendar. If you sized this against the Calendly API, please re-size it.
W1-9 Demo bot / carriers. Yes, consolidate on Telnyx — one carrier, one credential set, one CDR source. THE CONDITION: the demo bot must not run on the production 10DLC campaign. That campaign registers as Customer Care and Account Notification. A demo bot messaging a PM prospect fits neither and risks the registration the product depends on. Second Telnyx number, separate campaign registration, demo messages only after the prospect texts in first. A "text DEMO to this number" flow makes the consent clean and is a better demo than an unsolicited text.

 
Part 5 · Four things you have not asked yet
5.1 How Respond receives an inquiry, and who it sends as
These are one answer, and it removes work rather than adding it. Launch already gates on calendar-connect and W1-8 already commits to Google Calendar and Microsoft Graph. Mail scopes ride the same OAuth consent, the same provider, the same code path. One connection at onboarding grants calendar and mail together.
Primary path: read and reply through the client's own mailbox. Watch a filtered view — their web-form sender, or a label they apply — and reply in-thread as them. The reply is genuinely from them because it literally is: it lands in their sent folder, threads correctly, inherits their deliverability. No SPF, no DKIM delegation, no DNS step in onboarding.
Fallback: a dedicated Blackink address plus forwarding, replies from a subdomain they delegate. Needed for a firm that will not grant mailbox access or runs mail somewhere neither API reaches.
Phone inquiries need no build. The client turns on voicemail-to-email — standard on every phone system — and missed calls land in the same mail pipe. No voice agent, no transcription service, no new consent surface.
One split to write down now: Respond's 1:1 in-thread replies go through the client's mailbox. Bulk sending never does. Dead-book reactivation works hundreds of records at a time and would burn a client's Workspace reputation and hit their sending limits — that traffic goes out on a delegated subdomain with its own SPF and DKIM. The useful consequence is that DNS friction only ever touches Growth OS and dead-book clients, never the $249 entry product.
 
5.2 Where the rank's source data comes from

Rank v1 is public observable data only — that is what the ghost-shopper hold means in practice.

 
Signal
	Why it belongs
	Source
Review response rate
	The strongest signal we have. A public proxy for exactly the failure Respond fixes. "You reply to 12% of reviews; the top firm in your county replies to 89%" is credible and uncomfortable in the way the report needs to be.
	Business profile
Review count and recency
	Volume and freshness. Nothing in eighteen months means shrinking or invisible.
	Business profile
Average rating
	Blunt, but everyone already believes it, so omitting it costs credibility.
	Business profile
Owner-facing web presence
	A page aimed at owners not tenants, a working contact form, a phone number, a mobile layout. Deterministic checks, no judgement.
	Their site
Licence status and tenure
	Active broker licence and how long held. Free, authoritative, separates real operators from side businesses.
	State licence roll
Estimated portfolio size
	Door count from their own site claim or profile. Rough, but it bands every price downstream.
	Site + profile
Cut from v1: listing age and days vacant. The clean sources do not exist and the unclean ones violate site terms — the same reasoning that held the ghost shopper. Revisit when clients supply it from their own systems after onboarding.
Budget a real line for this. Business-profile data across ~12,000 Florida firms refreshed monthly is a low-hundreds monthly cost via the official API, less through a scraping provider. Confirm current pricing at build time rather than trusting this page. Cache aggressively — monthly refresh is plenty and the alert job only diffs against last month.

 
5.3 Acceptance criteria — one test per link

W0-1 has a proper test. Nothing else does. On a one-month build with a hard end date, a link without a test is a link that gets called finished while the chain is still broken.
 
 
	Link
	Done means
1
	See their own rank
	A named real firm's report generates end to end from a cold database, shows ≥5 scored signals with sources, states its own assumptions on the page, and two people reading it independently agree the rank is defensible.
2
	We can reach them
	A campaign sends to 100 verified addresses with bounce under 3%; a deliberately non-compliant draft is
blocked by the lint, not warned about; a suppressed address gets nothing even when added mid-campaign.
5
	They can pay
	A real card and a real ACH mandate both charge. Payment flips the entitlement row. An ACH return
pauses the entitlement rather than silently continuing service, and a failed charge raises a Slack notice.
6
	They get onboarded
	A test client completes Exhibit A → polygon → ACH mandate → calendar connect → owner-file upload with no manual step, and the state machine
refuses to advance with the owner file missing.
7
	Never email their owners
	Upload a client owner file containing a known test address; run a campaign across that county; the test address receives nothing. Re-run after a second file upload and confirm the union is respected.
8
	First inquiry answered
	A real inbound inquiry gets a correct reply in under five minutes, in-thread, from the client's own mailbox. A question about management fees is
refused by the topic blocklist and escalated, not answered.
9
	They see what they paid for
	A charge produces exactly one email carrying appointment id, band, parcel reference and proof link, with a dispute button that writes a dispute row and pauses that line item.
5.4 Lead agent topic blocklist

Lead speaks as the client firm. It never discusses management fees, lease terms, rent advice or screening criteria — quoting those is practising for them and making Fair Housing statements on their letterhead. Free text escalates; deterministic templates run at Band 2 from launch, because answering in under five minutes 24/7 is incompatible with per-send approval.

 
 
 
Part 10 · Six changes from an independent review

The plan was put through three separate adversarial reviews. Six things change. Two of them take work out of the build.
 
10.1 Respond uses a forwarding alias, NOT mailbox OAuth — read this first
Do not build Gmail or Microsoft Graph mailbox access. The client sets a forwarding rule on their owner-inquiry address pointing at a Blackink address. Replies go out from a Blackink-hosted address.
Four reasons, and they compound:
 
"We never touch the tenant side" is false the moment we hold read access to a general mailbox. That inbox carries tenant IDs, bank details, fair-housing accommodation requests, maintenance photos. We would receive all of it before classifying anything. A forwarding rule sends us only what the client points at us.
gmail.modify and gmail.readonly are Google restricted scopes. Shipping publicly with them needs app verification plus an annual third-party security assessment — weeks to months of calendar time. The fewer-than-100-users exemption does not apply; it is framed as personal use, for users known personally to the developer.
The Workspace-admin-trust route works but breaks self-serve — it needs the prospect to hold Google admin rights and complete a security step in their admin console. That is an IT ticket, not a checkout flow.
It is a far smaller ask of the customer. A forwarding rule is a two-minute mail setting. Inbox access is a conversation with their broker and their insurer.
Net effect: the OAuth mail work comes out entirely. Calendar OAuth stays — normal scope, and the attendance bar needs it.

 
10.2 The response SLA for early clients is 30 minutes, not 5

Client one has zero clean approvals, so every send needs a human and Band 2 is unreachable. "Under five minutes, 24/7" and "a human approves every send" cannot both be true in month one.
September promise: 30 minutes in business hours, next business morning after hours — tightening to five minutes once a template class has earned autonomy at 50 clean approvals. The five-minute claim returns when it is provable.

 
10.3 County exclusivity comes out of September

Priced against the county's actual worth rather than against Growth OS, it does not hold: at $567 subscription ARPU, both seats at $2,399 block a county worth $3,405 at six clients and $5,675 at ten. Remove the seat SKUs from the build. Keep them as disabled rows.
 
10.4 Appointment bands collapse from four to two

Nine doors at $350 and ten doors at $750 is a 114% jump for one door, on a field the owner self-reports. New bands: $250 for owners with 1–9 doors, $600 for 10+. $97 first appointment and $75 dead-book appointment unchanged.

 
10.5 Retention Guard has ONE add price, not three

Currently it carries $497 standalone, $400 as the checkout bump, and $349 as an "existing account upgrade." Delete the $349 — it lands at $598 and does not match the bundle. One add price of $400, used at checkout and as the upgrade, so an existing Respond account that upgrades lands on exactly $649. Label it on screen as the bundle discount.

 
10.6 Three things to build now because they are expensive later
One global suppression list across all sending domains. An unsubscribe against domain 7 must suppress that address on domains 1–30. Architecture, not copy — CAN-SPAM has no B2B exception.
A real offboarding path that revokes access, kills forwarding and deletes the owner file on a stated schedule.
Immutable raw events plus versioned corrections, not a table that refuses to repair a wrong fact. Append-only history is right; permanent factual errors are not.
 
 
 
The inputs — delivered
11.1 Reach: national, starting with two counties

To be explicit, because it has been ambiguous: the county is the unit, not the limit. Every firm in the country sits in a county; national simply means many of them.

 
Product
	Reach
Owner Visibility Score (free report)
	National from day one. Public data only, no licence, no counsel
Respond
	National from day one. Forwarding alias, their contacts, no local data
Retention Guard, dead book
	National from day one
Attended appointments
	Cleared counties only — contracted polygon, and counsel per state

So: score and sell everywhere; bill appointments only where we have cleared the ground.

 
11.2 County priority — the first two are already built
Hillsborough and Pinellas first, because the Forced Action public-records pipeline for those two counties exists and runs today. Anywhere else means new plumbing for no reason.

Then: Orange · Duval · Polk · Pasco · Lee · Brevard · Volusia · Seminole.
Deliberately not first: Miami-Dade, Broward, Palm Beach — biggest markets, but the most competitive, most Spanish-language dependent, and thickest with large firms, which are the hardest sale for a vendor with no logos yet.

 
11.3 The Owner Visibility Score — the formula

Renamed from "Owner Response Score." All three reviews independently found that review-reply behaviour measures whether a firm employs a reputation VA, not whether it answers owners — and with no pretext contact, the score cannot observe the thing the old name promised.

 
Category
	Signal
	Pts
Owner conversion readiness (30)
	An owner-addressed page exists, distinct from tenant pages
	14
 	Working owner contact form, phone and email all present
	10
 	Site health — mobile, HTTPS, loads under 3s
	6
Market visibility (28)
	Review count against the county median
	16
 	Review recency — under 60 days full, over 12 months zero
	12
Public reputation (26)
	Review response rate
	18
 	Average rating
	8
Accessibility (12)
	After-hours contact route published
	8
 	Median review response lag
	4
Credibility (4)
	Active broker licence and tenure
	4
Total
	 	100
Scale is removed from the score and displayed separately as a company-size band — it double-counted with review volume.
Every report carries a coverage figure. "76/100, 94% data coverage" is defensible; "63/100" with three signals silently missing is not.
Report layout: lead with the three lowest-scoring signals phrased as observations with a named comparison, then the score, then the county rank. The findings are undeniable; the score is the hook.
Publish the top 25 per county only. Never a bottom list. Firms below the data floor are marked "insufficient data" and are not ranked.

 
11.4 Exhibit A — what counts as a qualified owner meeting

This is the one-page attachment a client initials at signup. It is what every future billing dispute turns on.

A meeting is billable when all of the following are true of the person who attends:

 
They own, or are legally authorised to make management decisions for, at least one residential rental property inside the contracted county.
The property is held as an investment — not their primary residence, not listed for sale as an owner-occupied home.
They are not already under a management agreement with the Client.
Any existing management agreement with another firm has 60 days or fewer remaining, or is terminable at will.
Before the meeting was scheduled, they stated they are considering appointing or changing a property manager within the next 90 days. Their own words are recorded on the appointment record.
The property type is one the Client accepts: single family · duplex–fourplex · condo · townhouse · small multifamily 5–20 (client ticks).
The property is not commercial, raw land, a mobile home on a leased lot, or operated exclusively as a short-term rental.

Plus two client-set fields: client-specified exclusions (free text, never billed) and minimum door count for a billable meeting (default 1).

 
11.5 Stripe — create these fourteen and send the price IDs
Product
	Price
	Type
Respond
	$249/mo
	recurring
Respond + Retention Guard (bundle)
	$649/mo
	recurring
Retention Guard — add to existing
	$400/mo
	recurring
Retention Guard — standalone
	$497/mo
	recurring
Growth OS — founding rate, first 25
	$897/mo
	recurring
Growth OS — standard
	$997/mo
	recurring
Attended appointment — owner 1–9 doors
	$250
	one-time
Attended appointment — owner 10+ doors
	$600
	one-time
First paid appointment, once per customer
	$97
	one-time
Dead-book appointment
	$75
	one-time
Extra engine
	$97/mo
	recurring
Extra office
	$79/mo
	recurring
Dead-book engine
	$99/mo
	recurring
Additional county
	$397/mo
	recurring
Do not create the county seat products.

 
 
 
Four more things you have not asked for
Where do agent drafts go for approval? Campaign drafts to Slack for approve/reject needs a named channel and a named approver. If it is only Josh, what happens overnight? Propose a channel and a fallback.
What is the Blackink-side address a client forwards to? One per client, on which domain, and what happens if two clients pick the same alias.
The rank report is the most-seen thing we make. W1-3 says take Forced Action patterns and skip design review — that is right for internal screens and wrong for this one. Thousands of firms will judge us on this single PDF or page. It deserves real care.
Which real firm is the 11 September demo about? Pick it this week so the report can be generated and checked by a human before it is shown.
The Lead agent's words

Lead is the $249 product. It runs at Band 2 on deterministic templates and speaks as the client firm. Every template has the same shape: acknowledge, ask one qualifying question, offer two times, book.
 
1 · General enquiry — the most common
Hi {first_name} — thanks for reaching out to {client_firm}.
Yes, we manage properties like that. Before I put time on the calendar, one quick thing: how many rental properties do you own right now, and are any of them currently with another manager?
If it's easier to talk it through, I have {slot_1} or {slot_2} open this week — either works, just tell me which.
{signoff}
Qualifies: door count and existing management — the two fields Exhibit A turns on. Never: a fee figure, a rent estimate, or an opinion on their property's value.

 
2 · Asks about fees — blocked topic, escalates
Hi {first_name} — good question, and I'd rather {client_owner_name} answer that properly than give you a number that turns out to be wrong for your situation. Fees depend on the property and the service level.
Can I get you fifteen minutes with them? {slot_1} or {slot_2} this week.
{signoff}
Behaviour: books the meeting, flags the thread to the client, answers nothing. This is the most important template in the set — it is the one keeping the agent out of practising for the client.

 
3 · Unhappy with their current manager — highest intent
Hi {first_name} — sorry to hear it, and thanks for getting in touch.
Two things that decide how quickly we could help: how many properties are involved, and is there a notice period on your current agreement?
I've got {slot_1} and {slot_2} free — grab whichever suits.
{signoff}
Qualifies: door count and notice period — conditions 3 and 4 of Exhibit A, in the owner's own words before the hold. Never: a comment about the other firm, by name or otherwise.

 
The remaining seven, same shape
 
	Case
	Behaviour
4
	New investor, just bought
	Qualifier: how many units, and when does it need tenanting
5
	Out-of-state owner
	One line acknowledging remote ownership; qualifier unchanged
6
	Portfolio, several properties
	Flags for the principal appointment type, offers a longer slot
7
	Vacancy — "my place is empty"
	Books the meeting. Says nothing about marketing the unit or tenant criteria — that edges toward leasing
8
	Asks for a phone call
	Offers times. Never places a call — no agent-initiated outbound calls, ever
9
	Outside the service area
	Polite decline, no booking, logged as ICP-mismatch so it never counts as an appointment
10
	Tenant, vendor pitch or spam
	Routed to the client untouched. Lead never replies to a tenant. This line is absolute
Part 14 · The forwarding alias, and the two links with no test
14.1 Why the alias, and what it costs

Four reasons, compounding:

 
"We never touch the tenant side" is false the moment we hold read access to a general mailbox — that inbox carries tenant IDs, bank details, fair-housing requests and maintenance photos, and we would receive all of it before classifying anything.
gmail.modify and gmail.readonly are Google restricted scopes. Public use requires app verification plus an annual third-party security assessment — weeks to months of calendar time, not inside the 600 hours. There is no under-100-user exemption; that number is a cap on unverified apps in testing. The real exemptions are personal use, internal-to-your-own-Workspace, dev/staging and service accounts, and paying strangers are none of those.
The Workspace-admin-trust route works but breaks self-serve — it needs the prospect to hold Google admin rights and complete a security step in their admin console.
It is a far smaller ask. A forwarding rule is a two-minute mail setting; inbox access is a conversation with their broker and their insurer.
It does not cost output. Respond's job is to catch an owner enquiry, reply inside the SLA, qualify it and book it. Forwarding delivers the enquiry, so speed to first reply is untouched. Two small things are lost: whole-inbox coverage (mitigation — the client can point more than one address at the alias, and onboarding asks them to) and history on an owner who wrote in before we existed (mitigation — we build thread history from first contact forward).
Sending is the part to build carefully. Replies go out from a delegated subdomain with Reply-To set to the client's own address, and the client is BCC'd on every send, so it lands in their world and gives them an audit trail without us holding their mailbox. One DNS record at onboarding, not a per-message problem.

 
14.2 Links 3 and 4 have no acceptance test — here they are

5.3 writes one test per link for seven of the nine. Links 3 and 4 were skipped because they need no build, and "no build" quietly became "no proof." Both are in the chain and both can fail silently.
Link 3 — they can talk to us. A reply to a real campaign email reaches a human, is recorded against the firm it came from, and is answered inside the SLA. The failure this catches is the one nobody sees: a campaign that sends beautifully into an address nobody watches.
Link 4 — they can sign. A signature produces a complete acceptance record — agreement_version · accepted_at · accepted_by_email · ip · hash of the exact text rendered on screen — and that record can be retrieved by client afterwards. An agreement you cannot produce on demand is not an agreement.

 
BlackInk will be separate UI
 
 
````

<a id="source-b"></a>

# Source B — Project Blackink - Complete Implementation Blueprint (Full).pdf

Complete source transcription. Apply the authority rules above before treating any instruction or code as current.

<a id="source-b-page-1"></a>

## Source B — Page 1

````text
UNIFIED SYSTEM SPECIFICATION & IMPLEMENTATION BLUEPRINT

Project Blackink — Implementation Blueprint
Client                   Josh Kantor, Blackink
Lead Developer           Hari Krishnan (heu.ai)




1. Master Data Contracts & Ingestion Variable Specification

All upstream prospect and market intelligence delivered by the data pipeline must map directly to the raw_prospect_pipeline staging schema before ingestion into the primary database.

A. Target Company & Market Entity Schema
   company_id (UUID / String, Primary Key): Deterministic SHA-256 hash generated from normalized domain.

   company_name (String, NOT NULL): Legal operating or DBA name of the property management company.
   website (String, NOT NULL): Validated corporate website URL.
   domain (String, UNIQUE, NOT NULL): Normalized apex domain (e.g., suncoastpm.com ).
   market_metro (String, NOT NULL): Geographic target market (e.g., Tampa-St. Petersburg , Orlando , Miami-Dade ).
   door_count_est (Integer, NOT NULL): Estimated residential doors under management.
   current_pm_software (String, Nullable): Ingested primary PMS platform ( AppFolio , Buildium , Propertyware , Rent Manager , Other , UNKNOWN ).


B. Two-Contact Structure (Owner/Broker & Operations)
   contact_id (UUID / String, Primary Key): Unique identifier per contact.
   contact_role_type (Enum, NOT NULL): Explicit role tag ( OWNER_BROKER_MD for Contact A; OFFICE_MANAGER_OPS for Contact B).
   first_name (String, NOT NULL): Contact given name.
   last_name (String, NOT NULL): Contact family name.
   title (String, NOT NULL): Full professional title.
   email (String, NOT NULL): Direct corporate email address.
   email_status (Enum, NOT NULL): VERIFIED , ESTIMATED , UNVERIFIED , BOUNCED .
   phone (String, E.164 format, Nullable): Direct line or office telephone ( +1XXXXXXXXXX ).
   phone_type (Enum, NOT NULL): MOBILE , DIRECT_WORK , OFFICE_LANDLINE .
   linkedin_url (String, Nullable): Personal LinkedIn profile URL.


C. Data Lineage & Provenance Metadata
   source_channel (String, NOT NULL): Originating raw list source ( FL_DBPR_LICENSE , GOOGLE_MAPS_SWEEP , MANUAL_CURATED ).
   source_timestamp (ISO 8601 UTC, NOT NULL): Extraction datetime.
   enrichment_timestamp (ISO 8601 UTC, NOT NULL): Verification datetime.
   enrichment_provider (String, NOT NULL): Validation provider ( Hunter , Anymail , Tracerfy , Internal_Scraper ).


D. Audit & Personalization Payload
   audit_speed_score_sec (Integer, Nullable): Ghost-shopper response time recorded in seconds.

   audit_loss_dollars_est (Integer, Nullable): Modeled annual revenue loss based on response latency and door count.
   personalized_video_id (String, Nullable): Sendspark dynamic merge token.

   custom_hook_text (String, Nullable): Dynamic personalized opening hook for Email 1.


E. Compliance & Suppression Attributes
   suppression_state (Boolean, DEFAULT FALSE): Global suppression flag ( TRUE = locked from all outbound).

````

<a id="source-b-page-2"></a>

## Source B — Page 2

````text
   dnc_clean (Boolean, DEFAULT FALSE): Verified against National and Florida DNC registries.

   is_opted_out (Boolean, DEFAULT FALSE): Explicit opt-out recorded across any channel.
   compliance_eligibility (Enum, NOT NULL): EMAIL_COLD_ELIGIBLE , TRANSACTIONAL_SMS_ONLY , BLOCKED .


Ingestion Validation & "Ready for Campaign" Gate
A staged record automatically transitions to READY_FOR_CAMPAIGN only when all predicate checks pass:

 -- Execution Gate in src/compliance/gate_evaluator.py
 CREATE OR REPLACE FUNCTION evaluate_campaign_readiness(target_contact_id UUID)
 RETURNS BOOLEAN AS $$
 BEGIN
     RETURN EXISTS (
         SELECT 1 FROM raw_prospect_pipeline p
         JOIN companies c ON c.domain = p.domain
         WHERE p.contact_id = target_contact_id
           AND p.email_status = 'VERIFIED'
           AND p.is_opted_out = FALSE
           AND p.dnc_clean = TRUE
           AND p.suppression_state = FALSE
           AND p.compliance_eligibility = 'EMAIL_COLD_ELIGIBLE'
           AND (p.audit_speed_score_sec IS NOT NULL OR p.personalized_video_id IS NOT NULL)
           AND NOT EXISTS (
               -- Cross-client non-poach gate check
               SELECT 1 FROM client_pm_books b
               WHERE b.owner_domain = p.domain OR b.owner_email = p.email
           )
           AND (
               p.last_outbound_touch_at IS NULL
               OR p.last_outbound_touch_at <= NOW() - INTERVAL '14 days'
           )
     );
 END;
 $$ LANGUAGE plpgsql;

````

<a id="source-b-page-3"></a>

## Source B — Page 3

````text
2. Core Architectural Principles & Invariants

 Commercial Terms as Database Rows: No price, fee, tier, seat, escalator, credit, cap, or gate is compiled into an agent or hardcoded into application branches. Adding or updating any commercial offer is
 strictly an INSERT into the entitlement_offers table.
 The Outcome Table as System of Record: The generic events and outcomes datastores are constructed first. Every learning loop, offer trigger, dispute evaluation, audit trail, and communication log reads
 from or writes to this shared ledger.
 Engine Architecture (Row + Adapter): Every revenue engine is modeled as a database configuration row (defining router parameters, templates, sequence steps, county locks, tiers, and holdout percentages)
 coupled to a dedicated execution adapter.
 Deterministic Money and Compliance: No autonomous agent or LLM makes financial, billing, legal, or compliance decisions. The compliance gate, non-poach suppression, dispute rules, and Stripe settlement
 pipelines run on strict, unbypassable code.
 Tenant Isolation & Security: Data, sending reputation, credentials, and memory structures are strictly partitioned by client_id . Cross-client leakage tests run automatically in CI/CD.

````

<a id="source-b-page-4"></a>

## Source B — Page 4

````text
3. Master Week-by-Week Implementation Sprints

Week 0 (Aug 31 – Sept 2, 2026): Step 1 Reuse, Critical Platform Remediation & Compliance
Phase Objective: Gate 1 Proof, Fork & Baseline Stabilization

Week 0 serves as the foundational validation gate for the entire Blackink operating platform. Before deploying net-new campaign logic, commercial sequences, or client-facing onboarding tools, the technical team
isolates and audits all reusable infrastructure across prior builds. This phase forks core execution pipelines, eliminates known legacy defects, deploys the centralized Slack Agent Hub, establishes the upstream data
pipeline contract with Akrash, and files official carrier brand and campaign registrations. Week 0 operates as a strict proof gate: work focuses on establishing tenant isolation, persistent safety halts, deterministic
compliance gates, and verified truth states so that downstream marketing and settlement modules build upon a stabilized, bug-free platform.

3.0.1 Core Asset Audit & Module-by-Module Reuse Ledger
The development team executes a comprehensive code audit across existing agent frameworks, classifying components into distinct portability tiers:
  Direct Porting (Clean Fork):
     Outbound Drafting Patterns: Language models and structured output formatters from drafting engines.
     Event-Driven Execution Harnesses: Asynchronous queue runners, dispatchers, and state machines.
     Suppression & DNC Scrubbing: Deterministic matching routines connecting to state and national Do-Not-Call registries.
     Slack Interactive State Machines: Interactive Block Kit button handlers ( Approve , Revise , Reject , Snooze , Skip , Mark Done ) and modal submission listeners.
  Refactored Components:
     Data Normalization & Ingestion: Adapting entity resolution routines to separate generic properties from corporate LLC owners holding multi-unit portfolios.
     Outbound Dispatch Adapters: Decoupling pooled sending identities into strict, tenant-isolated mailbox assignments.
     Audit Compilation Pipelines: Modularizing PDF loss-report generators to consume property management speed metrics rather than general distress scoring.
  Pattern-Only Adoptions:
     Multi-Tenant Data Schema: Establishing row-level tenant keying ( client_id ) across all primary tables, views, and Redis cache keys.
     Deterministic Gate Enforcement: Hard-coded pre-send policy checks that completely bypass LLMs when evaluating suppression, quiet hours, and channel eligibility.
A formal Reuse Ledger is compiled and delivered at the conclusion of Week 0, documenting components ported, architectural refactors completed, and automated test pass rates.

3.0.2 Platform Defect Remediation & Core Infrastructure Hardening
To prevent legacy runtime bugs from propagating into Blackink's production environment, Week 0 addresses four critical system vulnerabilities:
  A. Vera Silent-Zero Remediation: In legacy reporting pipelines, database queries or API lookups returning null or missing financial values defaulted silently to 0 . In an outcome-based billing architecture where
  invoices trigger upon attended meetings and verified contracts, a silent zero results in unbilled revenue and corrupts accounting truth. The Fix: The data-reconciliation layer is refactored to enforce strict tri-state
  logic. When an external integration (Stripe, calendar logs, PM software read feeds) is unreachable, degraded, or returns missing values, the system explicitly returns UNKNOWN or ABSTAIN . Downstream settlement
  routines halt automatically and alert administrators rather than assuming zero payable activity.
  B. Relay Persistent Halt & TTL Re-Arm Patch: Legacy execution state machines contained a vulnerability where emergency pause commands would automatically re-arm and resume outbound message
  queues after an internal Time-To-Live (TTL) timer expired. The Fix: The scheduler and queue worker state machines are updated so that a global, client-level, or campaign-level pause persists indefinitely in Redis
  and PostgreSQL. Outbound queues remain locked until an authorized administrator explicitly executes a cryptographic resume command in Slack.
  C. Cora Queue Throttling & Batch Burst Protection: Draft generation engines previously lacked dynamic backpressure controls, causing draft queues to overwhelm human reviewers during large prospect
  batch ingestions. The Fix: Strict queue bounds and pacing throttles are embedded into draft orchestrators. Generation limits automatically pause new drafting once unreviewed Slack approval queues reach
  capacity, resuming only as human reviews clear items.
  D. Standalone Hunter Entity Resolution Deployment: To prevent heavy cross-table data joins from blocking the primary web application and API threads, entity resolution is decoupled. The Fix: A
  standalone worker droplet is provisioned exclusively for Hunter-style entity matching. It runs nightly asynchronous background sweeps, resolving corporate names, registered agents, and individual property
  owners across fragmented multi-property LLC portfolios.

3.0.3 Slack Agent Hub ( @Blackink ) & Interactive Cockpit Deployment
Week 0 establishes @Blackink as the centralized operating surface inside Slack, ensuring full visibility and control over all background workflows:

````

<a id="source-b-page-5"></a>

## Source B — Page 5

````text
 CENTRAL SLACK AGENT HUB ARCHITECTURE

                         ┌──────────────────────────────┐
                         │      @Blackink Router        │
                         │ (Command & Intent Dispatcher)│
                         └──────────────┬───────────────┘
                                        │
       ┌─────────────────┬──────────────┼───────────────┬─────────────────┐
       ▼                 ▼              ▼               ▼                 ▼
 #blackink-command #blackink-setter #sales-replies #dial-tasks      #blackink-qa &
 (Global Cockpit)   (Context Cards)   (Inbound Triage)(Call Tasks)   #blackink-economics



   Dedicated Operational Channels:
      #blackink-command : Executive overview, macro pipeline queries, active tenant statuses, and global pause/resume controls.

      #blackink-setter : Human conversation cockpit rendering 1-screen context cards for high-intent owner leads.
      #sales-replies : Real-time inbound reply stream routing prospect email/SMS responses with automated intent tags.

      #dial-tasks : Prioritized daily phone queue populated with company background, response latencies, and direct lines.

      #blackink-qa : Real-time system health logs, API heartbeat failures, domain reputation deltas, and cross-tenant leakage test alerts.

      #blackink-economics : Rollup dashboards tracking customer acquisition costs, channel unit economics, and wallet caps.

   Payload-Bound Hash Verification: Every interactive Slack card ( Approve , Revise , Reject , Snooze , Skip , Mark Done ) is cryptographically bound to a SHA-256 hash of the exact message payload, recipient
   identifier, and configuration state. If a draft payload or template is modified in the background while awaiting review, clicking an outdated Slack button is rejected by the backend, preventing stale or altered
   messages from executing.

3.0.4 A2P 10DLC Carrier Registration & Compliance Infrastructure
To ensure message deliverability and carrier regulatory compliance, Week 0 initiates the carrier verification clock through Telnyx/The Campaign Registry (TCR):
   Brand Registration: Legal Entity: HEU AI LLC. Address: 971 US Highway 202N Ste N, Branchburg, NJ 08876. Entity Type: Private Company, LLC (New Jersey). Vertical: Real Estate / Professional Services.
   Campaign Filing Details: Use Case Classification: Mixed: Customer Care + Account Notification (Strictly non-marketing to secure lower carrier fees and eliminate promotional vetting friction). Campaign
   Description: "Blackink provides scheduling and inbound-response services to residential property management firms. Messages are sent only to property owners who have (a) initiated an SMS conversation with us
   or (b) booked an appointment through us. Message types: appointment confirmations, reminders, rescheduling links, and replies to owner-initiated questions. No promotional or cold outreach is sent by SMS."
   Opt-In & Consent Description: "Opt-in occurs in one of two ways: (1) The property owner sends an SMS to a Blackink number, which constitutes consent to reply. (2) The property owner books an appointment
   via a web form or during a phone/email conversation and enters their mobile number, with a consent statement displayed at the point of collection: 'By providing your mobile number you agree to receive
   appointment confirmations and reminders by SMS. Reply STOP to opt out, HELP for help. Msg & data rates may apply.' No numbers are purchased, scraped, or imported from third-party lists for SMS."
   Keywords: STOP , END , CANCEL , UNSUBSCRIBE , QUIT for automated opt-out; HELP for support routing.
   Code-Level Compliance Enforcement: CI/CD build test fails immediately if code attempts an outbound SMS to any contact where inbound_sms_count == 0 AND booked_appointment_id IS NULL . Quiet hours strictly
   enforced (no SMS delivery between 9:00 PM and 8:00 AM recipient local time). Outbound templates require the {client_firm} tag, ensuring the recipient clearly sees the operating company name.

3.0.5 Section 8 Data Pipeline Interface & Akrash Staging Handoff
Week 0 finalizes the formal operational data contract between the upstream data team (Akrash) and the core platform (Hari), eliminating manual CSV spreadsheets:
   Staging Database Ingestion: Akrash is provisioned restricted access to write prospect data directly into the raw_prospect_pipeline PostgreSQL staging table.
   Mandatory Schema Fields: Ingested records must contain company_id , company_name , domain , market_metro , door_count_est , two contacts ( contact_role_type as OWNER_BROKER_MD or OFFICE_MANAGER_OPS ), verified
   email, direct phone, and audit timestamps.
   Ready for Campaign Gate: Upstream records remain in quarantine until background evaluators confirm email verification, DNC clearance, global opt-out clearance, and non-poach cross-suppression checks.
   DNS & Warmup Delegation: DNS access is delegated to configure SPF, DKIM, and DMARC across 20 dedicated domains (40 mailboxes), initializing domain warmup schedules ahead of campaign launch.

3.0.6 Week 0 Acceptance Criteria & Definition of Done
Week 0 concludes and clears Gate 1 when all of the following verifiable conditions are met:
1. Live Slack Cockpit: The @Blackink Slack app is active in the designated workspace, posting native action cards and processing interactive button clicks with payload-bound hash verification.
2. Defect-Free Health Reporting: Vera health jobs execute across staging databases, outputting UNKNOWN or ABSTAIN states on missing inputs with zero silent zero returns.
3. Verified Persistent Halt: An emergency pause triggered via Slack persistently halts background execution queues across server restarts until an authorized resume is executed.
4. Entity Resolution Pipeline: Hunter standalone workers successfully resolve a sample batch of property owner entities across fragmented LLCs.
5. Submitted A2P 10DLC Filing: Brand and Mixed Campaign registrations are officially submitted to TCR, with automated CI/CD tests blocking cold outbound SMS.
6. Signed Data Interface Contract: The Section 8 Data Interface Specification is approved, and the raw_prospect_pipeline database staging schema is live.
7. Documented Reuse Ledger: A complete module-by-module accounting of ported assets, architectural refactors, and test coverage is delivered.

````

<a id="source-b-page-6"></a>

## Source B — Page 6

````text
Week 1 (Sept 1 – Sept 11, 2026): Sprint 1 — Phase 0 + Marketing & Demo Layer
Milestone Standard: September 11 Marketing Live (Live Outbound Campaigns, Ghost-Shopper Audits, Sendspark Dynamic Video Hooks, Automated Booking, and Demo Kit Sandbox)

Week 1 transitions Blackink from foundational scaffolding into a live demand-generation engine. The primary objective is to make the outbound sales and marketing systems fully functional by September 11,
enabling live pitches, mystery-shop response audits, personalized video delivery, automated calendar bookings, and interactive software demonstrations on real prospect data. The build sequence decouples front-
end marketing and demo assets from downstream tenant settlement rails, allowing cold outreach and demo bookings to run against target property management companies in Florida while multi-tenant client
fulfillment and settlement engines are completed in parallel.

 WEEK 1 COMPLETE CAMPAIGN & DEMO LAYER ARCHITECTURE (GO-LIVE: SEPTEMBER 11, 2026)

 [Raw Prospect Data: 500-1,000 PMs] ──► [Deterministic Compliance Gate] ──► [Ghost-Shopper Inbound Bot]
                                                       │                                  │
                                          [Non-Poach / DNC / Waterfall]          [Logs Latency & Speed]
                                                       │                                  │
                                                       ▼                                  ▼
 [Personalized Outbound Sequences] ◄── [Sendspark Video Merge Engine] ◄── [Dynamic PDF Loss Report]
                │
                ├──► [Email 1: Audit + Video + Reply-YES Micro-Ask]
                ├──► [Day 1–2 Phone Call Task ──► Enqueued into Slack #dial-tasks]
                ├──► [Email 2: Fee-Stack Revenue Opportunity Map]
                ├──► [LinkedIn Deep-Link Handoff ──► Manual Clipboard Copy]
                ├──► [Email 3: Metro Speed Index & Market Rank Angle]
                └──► [SMS Nudge ──► Unlocks Strictly Post-Engagement]
                                │
                                ▼
                [Inbound Reply Bridge ──► Real-Time Slack #sales-replies]
                                │
                                ▼
                [Calendly / GCal Direct Booking ──► Show-Rate Reminder Cascade]
                                │
                                ▼
                [Pre-Demo Lead-In Email (30m Prior) + Rent Analysis Bot SMS Pitch Weapon]
                                │
                                ▼
                [Sales Demo Kit: Permanent Golden Client Sandbox Live Rehearsal]



3.1.1 Data Spine, Entity Models & Ingestion Pipeline
The foundation of Week 1 is the generic events ledger and the primary entity data model. Every downstream component—from compliance checks to outbound dispatch and meeting attribution—reads from and
writes to this schema.

````

<a id="source-b-page-7"></a>

## Source B — Page 7

````text
-- Location: src/db/migrations/001_core_spine.sql

-- 1. GENERIC EVENTS STREAM (Shared Ledger of Record)
CREATE TABLE events (
    event_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id UUID NOT NULL, -- Scoped per tenant; Blackink self-marketing uses dedicated internal client_id
    owner_id UUID,
    property_id UUID,
    campaign_id UUID,
    event_type VARCHAR(100) NOT NULL, -- 'touch_sent', 'reply_received', 'audit_generated', 'meeting_booked', 'partner_ryse_enrollment'
    source VARCHAR(50) NOT NULL,       -- 'cold_outbound', 'ghost_shopper', 'website_inbound', 'direct_mail'
    value_cents BIGINT DEFAULT 0,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    occurred_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 2. TARGET COMPANIES (Property Management Firms)
CREATE TABLE companies (
    company_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain VARCHAR(255) UNIQUE NOT NULL,
    company_name VARCHAR(255) NOT NULL,
    website VARCHAR(255) NOT NULL,
    market_metro VARCHAR(100) NOT NULL,
    door_count_est INTEGER NOT NULL DEFAULT 0,
    current_pm_software VARCHAR(100) DEFAULT 'UNKNOWN',
    status VARCHAR(50) DEFAULT 'PROSPECTING', -- 'PROSPECTING', 'ENGAGED', 'DEMO_BOOKED', 'CLIENT', 'EXCLUDED'
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 3. CONTACTS (Two-Contact Ingestion Model)
CREATE TABLE contacts (
    contact_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    contact_role_type VARCHAR(50) NOT NULL,
    first_name VARCHAR(100) NOT NULL,
    last_name VARCHAR(100) NOT NULL,
    title VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    email_status VARCHAR(50) NOT NULL DEFAULT 'UNVERIFIED',
    phone VARCHAR(50),
    phone_type VARCHAR(50) DEFAULT 'OFFICE_LANDLINE',
    linkedin_url TEXT,
    is_opted_out BOOLEAN DEFAULT FALSE,
    dnc_clean BOOLEAN DEFAULT FALSE,
    suppression_state BOOLEAN DEFAULT FALSE,
    compliance_eligibility VARCHAR(50) DEFAULT 'BLOCKED',
    last_outbound_touch_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- 4. PM PROFILE (Future-Proofing Metadata Schema)
CREATE TABLE pm_profiles (
    profile_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    company_id UUID UNIQUE NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
    specialty_tags TEXT[] DEFAULT '{}',
    languages_supported TEXT[] DEFAULT '{"English"}',
    asset_class_strengths TEXT[] DEFAULT '{"Single Family", "Small Multifamily"}',
    geographic_coverage_polygon JSONB,
    historical_close_rate NUMERIC(5,2) DEFAULT 0.00,
    average_speed_to_lead_seconds INTEGER DEFAULT 0,
    show_rate_percentage NUMERIC(5,2) DEFAULT 0.00,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

-- Indices for performance and compliance enforcement
CREATE INDEX idx_events_client_type ON events(client_id, event_type, occurred_at);
CREATE INDEX idx_contacts_lookup ON contacts(email, company_id, compliance_eligibility);
CREATE INDEX idx_companies_domain ON companies(domain);

````

<a id="source-b-page-8"></a>

## Source B — Page 8

````text
The ingestion pipeline processes 500–1,000 target property management companies across Florida metros (Tampa/St. Petersburg, Orlando, Miami-Dade). For every target enterprise, the pipeline resolves and
normalizes two distinct contacts:
1. Contact A ( OWNER_BROKER_MD ): The ultimate decision-maker (Managing Broker, Owner, President, CEO). Receives high-level financial proof: revenue loss metrics, fee optimization maps, and asset-growth angles.
2. Contact B ( OFFICE_MANAGER_OPS ): The operational gatekeeper (Operations Manager, Lead Property Manager, Leasing Director). Receives operational velocity proof: speed-to-lead benchmarks, mystery-shopping
   response timings, and software response gap metrics.
Data from the upstream raw_prospect_pipeline staging table is validated against apex domain uniqueness, normalized, and mapped into companies and contacts records.

3.1.2 Deterministic Compliance Gate & Non-Poach Architecture
Compliance in Blackink is hard-coded into deterministic execution gates. No artificial intelligence or language model is permitted to evaluate consent, opt-out rules, or cross-client suppression.

 DETERMINISTIC COMPLIANCE & WATERFALL FLOW

                   [Prospect Record Enters Execution Gate]
                                     │
                                     ▼
                  ┌──────────────────────────────────────┐
                  │ 1. Global / Explicit Opt-Out Check   │ ──► [FAILED] ──► [PERMANENTLY BLOCKED]
                  └──────────────────┬───────────────────┘
                                     │ [PASSED]
                                     ▼
                  ┌──────────────────────────────────────┐
                  │ 2. Cross-Client Non-Poach Validation │ ──► [MATCH] ──► [SUPPRESSED & LOGGED]
                  └──────────────────┬───────────────────┘
                                     │ [PASSED]
                                     ▼
                  ┌──────────────────────────────────────┐
                  │ 3. DNC Registry & Quiet Hours Check │ ──► [FAILED] ──► [CHANNEL SUPPRESSED]
                  └──────────────────┬───────────────────┘
                                     │ [PASSED]
                                     ▼
                  ┌──────────────────────────────────────┐
                  │ 4. Warm-Channel Waterfall Routing    │
                  └──────────────────┬───────────────────┘
                                     │
           ┌─────────────────────────┴─────────────────────────┐
           ▼                                                   ▼
 [Cold Outbound Tier]                                 [Engaged / Booked Tier]
 - Email: ELIGIBLE (Verified Email Only)              - Email: ELIGIBLE
 - Human Phone: Task to #dial-tasks                   - Transactional SMS: UNLOCKED
 - Cold SMS: STRICTLY BANNED (CI Fails Build)          - 10DLC Customer Care Approved



   Warm-Channel Waterfall: Cold prospect outreach is strictly restricted to Email and human telephone tasks. Cold outbound SMS is blocked at the database, application, and CI/CD testing levels. SMS permissions
   unlock strictly after an inbound message is received or an appointment is confirmed on the calendar.
   Cross-Client Non-Poach Gate: Read-only connections to active clients' property management software sync current owner rosters into a centralized suppression table. The compliance gate verifies that target
   domains, owner names, and entity parcels do not match any active client's book, preventing one client from poaching doors from another.
   Metro Allocation Algorithm: Where multiple property management firms operate within the same metropolitan boundary, target owners are allocated to one client campaign at a time based on portfolio size
   and operational fit. If outreach remains unacted upon for 30 days, allocation re-evaluates via an automated timer.
   National & State DNC Scrubbing: Automated pre-send linter queries real-time DNC registries, stripping dial tasks and SMS eligibility from restricted records.

3.1.3 The Outbound Proof Machine: Audit Factory, Personalized Video & Fee-Stack Generator

````

<a id="source-b-page-9"></a>

## Source B — Page 9

````text
 GHOST-SHOPPER AUDIT FACTORY & SENDSPARK DYNAMIC MERGE PIPELINE

 [Target PM Website Ingestion] ──► [Ghost-Shopper Inbound Bot] ──► [Submits Structured Owner Inquiry]
                                                                                 │
                                                                    [Captures Exact Milliseconds]
                                                                                 │
                                                                                 ▼
 [Dynamic PDF Loss Compiler] ◄── [Calculates Annual Revenue Loss] ◄── [Response Latency Recorded]
             │
             ├──► Injects: Metro Rank, Average Peer Speed, Lost Management Fees
             │
             ▼
 [Sendspark Video Integration Engine]
             │
             ├──► Generates Dynamic Video Landing Page: https://watch.blackink.io/v/{company_id}
             ├──► Injects Merge Parameters: {company_name}, {loss_score}, {audit_speed_sec}
             ├──► Renders Animated GIF Thumbnail with Prospect Website Overlay
             │
             ▼
 [Outbound Dispatch: Embeds Merge Token & PDF Attachment into Email 1 Payload]



A. Ghost-Shopper Agent — An automated headless crawler navigates to the target property management firm's public website, locates their owner inquiry or contact form, and submits a standardized,
professional owner inquiry. The bot records the exact millisecond of submission and establishes an inbound webhook and IMAP email listener. When the target company replies, the listener captures the timestamp,
calculates total elapsed seconds ( audit_speed_score_sec ), and logs the raw interaction into the events table. If no reply is detected within 24 hours, the record is flagged as UNRESPONSIVE_OVER_24H .
B. Dynamic PDF Loss Report Compiler — Consumes the ghost-shopper latency score and calculates modeled annual revenue leakage:

                                                                                  Estimated Lost Inquiries = Estimated Monthly Leads × (1 − e−0.0005 × Latency Seconds)

                                                          Annual Lost Revenue = Estimated Lost Inquiries × (Average Monthly Management Fee × 12) × Average Door Retention (Years)

Compiles a branded, 2-page executive PDF report detailing: (1) exact timestamped audit log of their form submission vs. first response; (2) geographic metro comparison (e.g., "You responded in 4 hours 12 minutes;
Top 10% in Tampa respond in under 9 minutes"); (3) estimated annual management and leasing revenue lost to competing managers who respond faster.
C. Sendspark Dynamic Video Merge-Tag Integration — Integrates with Sendspark via REST API to eliminate heavy, slow on-server video rendering. Dynamically generates personalized video landing pages
using merge-field parameters (URL pattern: https://watch.blackink.io/v/{company_id}?company={company_name}&speed={audit_speed_score_sec}&loss={audit_loss_dollars_est} ). Embeds an animated GIF thumbnail in
outbound emails showing a dynamic preview of their website overlaid with their speed audit score. Sendspark engagement webhooks ( video_watched_50_percent , video_completed ) fire into Blackink's webhook
receiver, logging events and notifying sales reps in real time.
D. Fee-Stack One-Pager Generator (ADD-8-Lite) — Discovery proof artifact mapping uncollected fee lines and ancillary margins using the shared templated merge pipeline. Highlights common fee leakage
points in property management (uncollected lease renewal fees, maintenance markups, tenant setup fees, pet rent share, and resident benefits packages). Visualizes the immediate revenue lift achieved by
activating ancillary partner programs (such as Ryse rent advances and utility concierge integrations).

3.1.4 Multi-Touch Outbound Sequencer & Human Bridge

Sequence Step             Channel & Mechanism                       Timing                 Content Focus & Psychological Angle                                                            Verification & Governance Rule

 Touch 1                  Cold Email (Direct)                       Day 0                  Speed Loss Audit + Sendspark Video: Delivers personalized PDF audit, dynamic video             Verified corporate email only; tracking pixel active.
                                                                                           link, and low-friction micro-ask ("Reply YES to see where you rank in Tampa").

 Touch 2                  Human Phone Call (Slack Task)             Day 1–2                Audit Follow-up Call: Automated prompt created in #dial-tasks for setter/closer to             Verified direct line; local calling hours enforced.
                                                                                           reference video view data and response audit findings.

 Touch 3                  Cold Email (Direct)                       Day 4                  Fee-Stack Opportunity: Introduces the ADD-8-Lite fee analysis showing uncollected              Threaded to Email 1; verifies no prior opt-out or reply.
                                                                                           ancillary revenue and Ryse rent advance partnership benefits.

 Touch 4                  LinkedIn Deep-Link (Manual)               Day 7                  Executive Peer Networking: Generates target profile URL and copies tailored connection         Logged as manual task; no headless browser automation.
                                                                                           note to setter's clipboard with zero automated browser scraping.

 Touch 5                  Cold Email (Direct)                       Day 10                 Metro Speed Index & Scarcity: References final quarterly Speed Index publication and           Final cold email touch before entering 30-day cooling.
                                                                                           announces upcoming market territory locks.

 Conditional              Engaged SMS Nudge (Direct)                Post-Engage            Direct Scheduling Nudge: Short SMS sent only after recipient clicks an audit link, watches a   Blocked unless explicit engagement event is logged in DB.
                                                                                           video, or replies positively to email.


Interim Reply Bridge & Task Routing (September 11–16): To bridge the gap before the automated Reply Triage Agent deploys in Week 2, an interim real-time routing engine handles incoming prospect
communication — an Inbound Reply Webhook Receiver ingests incoming prospect email and SMS replies instantly; Slack Routing ( #sales-replies ) posts an interactive alert card displaying prospect name, company
domain, door count, full message thread history, and one-tap action buttons ( Reply in Thread , Book Meeting , Mark Opt-Out ); the Setter Queue Bridge ( #dial-tasks ) populates phone tasks with direct dial numbers,
local timezone calculations, and Sendspark video watch percentage.

3.1.5 Inbound Conversion, Booking Engine & Show-Rate Cascade

````

<a id="source-b-page-10"></a>

## Source B — Page 10

````text
 BOOKING FLOW & SHOW-RATE CASCADE ARCHITECTURE

 [Self-Serve Audit Page / Email CTA] ──► [Calendly / Google Calendar Booking Form]
                                                           │
                                          [Webhook Captures: Name, Work Email, Mobile, Door Count]
                                                           │
                                                           ▼
                                          [Creates `meeting_booked` Event in Database]
                                                           │
                                                           ▼
                                          ┌──────────────────────────────────────────────────────┐
                                          │ 1. Instant Branded Confirmation Email + Calendar ICS │
                                          │ 2. Automated SMS Confirmation (Transactional A2P)   │
                                          └────────────────────────┬─────────────────────────────┘
                                                                   │
                                    ┌──────────────────────────────┴──────────────────────────────┐
                                    ▼                                                             ▼
                     [24 Hours Before Meeting]                                      [Morning of Meeting (8:00 AM)]
                     - Email Reminder with Prep Context                             - Short SMS Ping
                     - Links to Seeded Demo Video                                   - One-Tap Reschedule / Cancel Link
                                    │                                                             │
                                    └──────────────────────────────┬──────────────────────────────┘
                                                                   │
                                                                   ▼
                                             [30 Minutes Before Scheduled Meeting Time]
                                                                   │
                                                                   ▼
                                             [PRE-DEMO LEAD-IN EMAIL AUTO-DISPATCHED]
                                             - Prospect's Custom Audit PDF Attached
                                             - Rent Analysis Bot Phone Number Provided
                                             - Action Prompt: "Text any property address to test live"



  Self-Serve Audit Landing Page ( 3.6-Pixel ): High-converting public landing page where PMs can enter their corporate domain to request a certified speed audit. Automatically triggers background ghost-
  shopper workers, captures inbound lead details, and redirects high-intent prospects to the calendar booking interface. Embedded Meta and Google pixels build qualified retargeting audiences under strict wallet
  budgets.
  Show-Rate Reminder Chain: Automated email and transactional SMS confirmations sent immediately upon booking; 24-hour reminder email highlighting agenda and market-specific growth benchmarks; same-
  morning SMS ping (8:00 AM contact local time) confirming rep availability and providing a one-click rescheduling link.
  No-Show & Reschedule Handler: If a prospect fails to attend within 10 minutes of scheduled start, rep triggers Mark No-Show in Slack. Automatically pauses outreach sequences and enqueues a multi-channel
  recovery flow offering friction-free calendar re-booking.

3.1.6 Sales Demo Kit & Rent Analysis Bot Minimum Real Version

 RENT ANALYSIS BOT (`BOT-MIN`) LIVE DEMO WEAPON

 [Prospect Texts Property Address During Live Pitch: "123 Ocean Dr, Tampa FL"]
                                 │
                                 ▼
          [Twilio Webhook Ingests SMS to /api/v1/rentbot/demo]
                                 │
                                 ▼
          ┌─────────────────────────────────────────────────────────────┐
          │ Dynamic Address Parsing & Normalization Engine             │
          └──────────────────────────────┬──────────────────────────────┘
                                         │
                  ┌──────────────────────┴──────────────────────┐
                  ▼                                             ▼
        [Live Real-Estate Data API]                   [Demo-Mode Fallback Cache]
        - CoreLogic / RentCast Call                   - Pre-Computed Metro Valuations
        - Response Time: ~15-30s                      - Response Time: <5s (Guaranteed)
                  │                                             │
                  └──────────────────────┬──────────────────────┘
                                         │
                                         ▼
          ┌─────────────────────────────────────────────────────────────┐
          │ Automated SMS Valuation Reply Dispatched in <60 Seconds:    │
          │ "123 Ocean Dr, Tampa: Est Rent $2,450/mo (Range $2.3k-$2.6k)│
          │ Confidence Score: 94%. Powered by Blackink Rent Engine."    │
          └─────────────────────────────────────────────────────────────┘



  Permanent Demo Friday Sandbox Client: Formally designated, permanent test client environment populated with realistic operational data. Pulls up live Looker dashboards, active mock campaigns, lead-
  matching logs, and sample performance evidence packets on demand during sales calls.
  Pre-Demo Lead-In Automation: Fires automatically exactly 30 minutes before any scheduled sales demonstration. Emails the prospect their compiled Speed & Revenue Loss Report, provides the dedicated
  phone number for the Rent Analysis Bot, and instructs them to text a residential address to test the inbound AI live.

````

<a id="source-b-page-11"></a>

## Source B — Page 11

````text
   Rent Analysis Bot Minimum Real Version ( BOT-MIN ): A dedicated Twilio phone number running an SMS webhook receiver. Parses inbound property addresses, queries rental valuation APIs, and returns
   estimated monthly rent, confidence scores, and local rent comps within 60 seconds. Demo-Mode Fallback Cache: If an external valuation API times out or fails during a live pitch, the bot detects demo mode
   and serves an accurate, pre-computed local valuation from cache in under 5 seconds, ensuring the live demo never fails.

3.1.7 Day-One Learning & Pipeline Metrics Engine
Every interaction in Week 1 generates structured entries in the events table:

 // Example: Structured Touch Payload logged to events table
 {
   "event_id": "8f3b2d1e-9a4c-4b5d-8e7f-1a2b3c4d5e6f",
   "client_id": "00000000-0000-0000-0000-000000000001",
   "company_id": "3c4d5e6f-7a8b-9c0d-1e2f-3a4b5c6d7e8f",
   "contact_id": "5e6f7a8b-9c0d-1e2f-3a4b-5c6d7e8f9a0b",
   "event_type": "outbound_touch_dispatched",
   "source": "cold_outbound_sequencer",
   "payload": {
     "touch_step": 1,
     "channel": "email",
     "recipient_email": "j.smith@suncoastpm.com",
     "template_version": "v1.4_speed_audit_video",
     "ghost_shopper_speed_sec": 15120,
     "sendspark_video_id": "spk_99281a",
     "sending_domain": "growth-blackink.com",
     "mailbox_id": "mbx_04"
   },
   "occurred_at": "2026-09-02T14:32:10Z"
 }


60-Second Post-Meeting Form: To capture sales conversation outcomes from appointment #1, closers complete a 60-second Slack modal following every completed meeting, capturing Meeting Attendance Status
( Held , No-Show , Rescheduled ), Target PM Software, Estimated Door Count, Stated Objections ( Pricing , Software Integration , Capacity , Existing Agency ), and Next Action — feeding directly into the Owner Score
ranking engine and Prospecting Agent.
Pipeline Reporting & Slack Metrics Digest ( 1.6-Pipe ): Real-time Looker Studio dashboards connected directly to PostgreSQL read-replicas, with an automated daily morning Slack digest posted to #blackink-
command summarizing audits completed, average metro response latency, cold emails dispatched, open/click-through/video completion rates, and appointments booked.

3.1.8 Week 1 Milestone Definition of Done
The Sprint 1 / Marketing Live milestone is officially cleared on September 11, 2026, upon successful execution of the following live demonstration contract:
1. Live Outbound Dispatch: Demonstrate automated multi-touch email sequencing dispatching from warmed Google Workspace/Outlook inboxes across dedicated domains with verified SPF/DKIM/DMARC.
2. End-to-End Ghost-Shopper Audit: Submit a live test inquiry through a target property management website; capture the response latency in seconds; generate a branded, dynamic Speed & Revenue Loss PDF;
   and verify that Sendspark personalized video parameters merge cleanly.
3. Interactive Booking Flow: Complete a live meeting booking through the Calendly/Google Calendar integration; verify creation of the meeting_booked event in PostgreSQL; and confirm immediate delivery of
   email and transactional SMS confirmations.
4. Lead-In Automation & Rent Analysis Bot: Trigger the 30-minute pre-demo lead-in email; text a live Florida residential address to the Rent Analysis Bot phone number; and verify an accurate SMS rental
   valuation reply in under 60 seconds.
5. Interactive Slack Hub: Execute approval, revision, and snooze actions inside #blackink-setter and #sales-replies , verifying that payload-bound hash security blocks altered payloads.
6. Zero SMS Outbound to Unconsented Contacts: Execute the automated CI/CD compliance suite, demonstrating that cold outbound SMS is hard-blocked at the gate and rejected by runtime linters.

````

<a id="source-b-page-12"></a>

## Source B — Page 12

````text
Week 2 (Sept 14 – Sept 18, 2026): Sprint 2A — Settlement, Triage Agent & Founding Client Pilot
Milestone Standard: September 16–18 Founding Client Pilot Ready (Automated Inbound Reply Classification, Zero-Deposit Card Authorization & Settlement Rails, Tenant-Isolated Deliverability, and Hand-Assisted Tenant
Deployment)

Week 2 builds the core fulfillment, settlement, and service architecture that powers customer monetization and tenant operations. While Week 1 focused on outbound market demand, audits, and sales demos, Week
2 establishes the closed-loop machinery that manages incoming prospect intent, processes performance-based Stripe billing, executes core revenue recipes, and deploys the first founding client tenant.

 WEEK 2 COMPLETE CORE FULFILLMENT & SETTLEMENT PIPELINE

                [Inbound Replies / Webhooks / Portal Leads]
                                    │
                                    ▼
                ┌──────────────────────────────────────┐
                │ Reply Triage Agent (Classification) │ ──► [Intent Taxonomy: 10 Classes]
                └──────────────────┬───────────────────┘
                                   │
          ┌────────────────────────┼────────────────────────┐
          ▼                        ▼                        ▼
 [High-Intent Lead]      [Objection / Question]   [Unsubscribe / Opt-Out]
 - Setter Context Card   - Auto-KB Response       - Deterministic DNC Suppression
 - SLA Timers (15/60m)   - Thread Routing         - Global Opt-Out Sync
 - Booking Bridge        - Human Review Queue     - Sequence Immediate Halt
          │
          ▼
 [Completed Appointment / Signed Agreement]
          │
          ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────┐
 │ Deterministic Settlement Engine & Stripe Rails                                       │
 ├──────────────────────────────────────────────────────────────────────────────────────┤
 │ 1. Zero-Deposit Card Auth + ACH Mandate Storage                                      │
 │ 2. Nightly PM Read-Only Verification Sync (Syncs Doors/Agreements)                   │
 │ 3. 50/50 Settlement Trigger: 50% Charged at Signature, 50% Charged at Day 60          │
 │ 4. Automated 60-Day Clawback Monitor (Voids Back-Half if Churned)                    │
 │ 5. Dynamic Evidence Packet PDF Auto-Attached to Every Invoice                        │
 └──────────────────────────────────────────────────────────────────────────────────────┘
          │
          ▼
 [Founding Client Pilot Environment: September 16–18 Live Deployment]



3.2.1 Reply Triage Agent ( 1.4-Triage ): Inbound Intent Classification & Routing
The Reply Triage Agent eliminates manual inbox monitoring by classifying inbound communication across all channels (Email, SMS, Website Forms) and executing appropriate workflow actions within seconds.

Intent Class                   Description & Context Patterns                                                     Automated Platform Action                                                                  Routing Destination

 HOT_LEAD                      Explicit buying interest ("Let's talk", "Call me tomorrow", "How much do you       Halts cold sequence; extracts phone/availability; generates 1-screen context card.         #blackink-setter & instant SMS to rep.
                               charge?").

 QUESTION                      Informational queries regarding pricing, contract terms, software compatibility,   Evaluates knowledge base; drafts auto-response if confidence ≥90%, else queues             Thread in #sales-replies .
                               or service areas.                                                                  for human review.

 OBJECTION                     Pushback on timing, current satisfaction, pricing, or internal capacity ("We       Pulls objection handling playbook; equips Setter Copilot with counter-arguments.           Context card in #blackink-setter .
                               already have an in-house team").

 LATER                         Timing delay with future re-engagement signal ("Reach out in Q1", "Lease           Ingests date into Reactivation & Nurture timing memory; pauses active campaign             Reactivation queue.
                               expires in December").                                                             until target date.

 NURTURE                       Mild interest without immediate commitment ("Send more info", "Add me to           Transitions contact to low-frequency monthly educational nurture sequence.                 Nurture campaign stream.
                               your newsletter").

 UNSUBSCRIBE                   Requests for removal ("Remove me", "Stop emailing", "Take me off your list").      Executes deterministic opt-out; writes is_opted_out=TRUE across entity and                 Compliance ledger (Zero human touch).
                                                                                                                  domain suppression tables.

 COMPLAINT                     Aggressive or dissatisfied responses regarding outreach frequency or cold          Immediately halts sequence; logs complaint in QA monitor; suppresses apex                  #blackink-qa .
                               contact.                                                                           domain globally.

 LEGAL_GRIEF                   Threats of legal action, TCPA/CAN-SPAM citations, or regulatory complaints.        Hard circuit-breaker trip; freezes all company contacts; alerts executive Slack            #blackink-command (Priority P0).
                                                                                                                  immediately.

 WHALE_OWNER                   Prospect identified as managing or owning a large portfolio (≥50 units or multi-   Enforces VIP routing; triggers instant phone notification to closer; locks high-priority   Direct closer alert + #blackink-setter .
                               property LLC).                                                                     SLA timer.

 PARTNER                       Inquiries from Realtors, vendors, lenders, or property management service          Routes to Referral Agent; categorizes partner type for reciprocal introduction             #client-growth (Referral Desk).
                               providers.                                                                         workflows.

````

<a id="source-b-page-13"></a>

## Source B — Page 13

````text
 -- Schema: src/db/migrations/002_triage_routing.sql

 CREATE TABLE inbound_messages (
     message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     client_id UUID NOT NULL,
     contact_id UUID REFERENCES contacts(contact_id),
     channel VARCHAR(20) NOT NULL, -- 'EMAIL', 'SMS', 'WEB_CONCIERGE'
     raw_payload TEXT NOT NULL,
     cleaned_body TEXT NOT NULL,
     detected_intent VARCHAR(50) NOT NULL,
     confidence_score NUMERIC(5,2) NOT NULL,
     requires_human_review BOOLEAN DEFAULT FALSE,
     sla_due_at TIMESTAMP WITH TIME ZONE,
     status VARCHAR(50) DEFAULT 'RECEIVED',
     received_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );

 CREATE TABLE knowledge_base_entries (
     entry_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     topic VARCHAR(100) NOT NULL,
     trigger_patterns TEXT[] NOT NULL,
     approved_response_template TEXT NOT NULL,
     min_confidence_threshold NUMERIC(5,2) DEFAULT 0.90,
     is_active BOOLEAN DEFAULT TRUE,
     created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );


Confidence Thresholding: If classification confidence is ≥90% and matches an approved knowledge base entry, the system prepares an automated response payload. If confidence is <90% or the intent is
classified as OBJECTION , LEGAL_GRIEF , or HOT_LEAD , the message bypasses auto-responders and creates an urgent task in Slack.
Setter Context Card Generation: For HOT_LEAD and WHALE_OWNER classifications, the agent compiles an instant context card into #blackink-setter containing prospect full name, verified title, company name, and
estimated door count; summary of ghost-shopper speed audit latency and estimated annual revenue loss; Sendspark video watch analytics; complete previous touch history; and suggested opening talk-track with a
direct one-click calendar booking link.
SLA Escalation Timers: Inbound hot leads trigger strict response timers — 15 minutes: unclaimed hot leads generate a second high-priority ping in Slack; 60 minutes: escalates to executive mobile notification; 240
minutes: automatically reallocates lead to the backup closer queue.

3.2.2 Stripe Card Authorization & Settlement Rails ( 1.7-Stripe )
Blackink operates on a verified success-only economic model. Clients pay zero upfront fees, zero onboarding retainers, and zero monthly minimums prior to verified results.

 ZERO-DEPOSIT ONBOARDING & DETERMINISTIC SETTLEMENT PIPELINE

 [Client Signs Agreement] ──► [Onboarding Flow Captures Stripe Card Auth + ACH Mandate Storage]
                                                           │
                                             [ZERO DOLLARS CHARGED UPFRONT]
                                                           │
                                                           ▼
 [Campaigns Live] ──► [Nightly PMS Sync Confirms New Signed Management Agreement: `door_signed`]
                                                           │
                                                           ▼
                  ┌─────────────────────────────────────────────────────────────┐
                  │ 50/50 Billing Split Engine Executes:                        │
                  │ 1. Triggers First 50% Charge via ACH (Card Backup)          │
                  │ 2. Compiles Dynamic Evidence Packet PDF                     │
                  │ 3. Schedules 60-Day Clawback Verification Job in PostgreSQL │
                  └──────────────────────────────┬──────────────────────────────┘
                                                 │
                                                 ▼
                  ┌─────────────────────────────────────────────────────────────┐
                  │ At Day 60: Automated PMS Verification Check Runs            │
                  ├──────────────────────────────┬──────────────────────────────┤
                  │ [Agreement Active in PMS]    │ [Agreement Cancelled <60d]   │
                  │ ──► Charge Remaining 50%     │ ──► Auto-Void Back-Half 50% │
                  │ ──► Email Receipt + PDF      │ ──► Log Clawback Event to DB │
                  └──────────────────────────────┴──────────────────────────────┘



A. Card Authorization & ACH Mandate Capture — During onboarding, the client submits credit card and bank account details through a secure Stripe Elements modal. A temporary $1 authorization hold verifies
card validity and is immediately released. A persistent Stripe setup_intent establishes an ACH Direct Debit mandate ( pm_ach_debit_mandate_id ); ACH serves as the primary billing rail for five-figure invoice volumes to
eliminate credit card processing fees, with the authorized credit card retained as secondary backup.
B. The 50/50 Settlement Split Mechanics — When a management agreement is verified, Blackink invoices the performance bounty or qualified appointment fee across two equal installments: Installment 1 (50%

````

<a id="source-b-page-14"></a>

## Source B — Page 14

````text
at Signature) charged immediately upon nightly verification; Installment 2 (50% at Day 60) placed into an automated scheduling queue in PostgreSQL.
C. Automated 60-Day Clawback Trigger — The verification daemon cross-references active client rent rolls and property management agreements every 24 hours. If a newly signed property is terminated,
cancelled, or offboarded within 60 days, the scheduled second 50% installment is automatically voided in Stripe. A settlement_clawback_executed event is logged, attaching the PMS cancellation record and notifying
Josh and the client via email.
D. Dynamic Evidence Packet Compiler — Every charge automatically compiles and attaches a comprehensive PDF Evidence Packet: Section 1 (Source & Outreach Lineage), Section 2 (Engagement & Booking
Record), Section 3 (Meeting & Qualification Verification), Section 4 (PMS Contract Verification).

 -- Schema: src/db/migrations/003_settlement_ledger.sql

 CREATE TABLE settlement_transactions (
     transaction_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     client_id UUID NOT NULL,
     owner_id UUID NOT NULL,
     property_id UUID NOT NULL,
     door_count INTEGER NOT NULL DEFAULT 1,
     total_bounty_cents BIGINT NOT NULL,
     installment_1_cents BIGINT NOT NULL,
     installment_2_cents BIGINT NOT NULL,
     installment_1_status VARCHAR(50) DEFAULT 'PENDING',
     installment_2_status VARCHAR(50) DEFAULT 'SCHEDULED',
     installment_1_charged_at TIMESTAMP WITH TIME ZONE,
     installment_2_scheduled_for TIMESTAMP WITH TIME ZONE,
     installment_2_charged_at TIMESTAMP WITH TIME ZONE,
     evidence_packet_url TEXT NOT NULL,
     stripe_invoice_id VARCHAR(100),
     is_clawed_back BOOLEAN DEFAULT FALSE,
     created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );


3.2.3 Tenant-Isolated Sending Reputations
Blackink avoids shared From-Name pools. To protect deliverability under performance-based pricing, email and SMS sending infrastructure is strictly isolated at the tenant level.

 TENANT-ISOLATED SENDING POOL ARCHITECTURE (20 DOMAINS / 40 MAILBOXES)

 ┌──────────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┐
 │ Blackink Internal Outbound (5 Domains / 10 Mailboxes)    │ Client Outbound Dedicated Pools (15 Domains / 30 Mailboxes)│
 ├──────────────────────────────────────────────────────────┼───────────────────────────────────────────────────────────┤
 │ - growth-blackink.com (2 Mailboxes)                      │ - Client 1 Dedicated Pool: 3 Domains (6 Mailboxes)         │
 │ - connect-blackink.com (2 Mailboxes)                     │ - Client 2 Dedicated Pool: 3 Domains (6 Mailboxes)         │
 │ - audit-blackink.com (2 Mailboxes)                       │ - Client 3 Dedicated Pool: 3 Domains (6 Mailboxes)         │
 │ - pm-blackink.com (2 Mailboxes)                          │ - Client 4 Dedicated Pool: 3 Domains (6 Mailboxes)         │
 │ - scale-blackink.com (2 Mailboxes)                       │ - Client 5 Dedicated Pool: 3 Domains (6 Mailboxes)         │
 └──────────────────────────────────────────────────────────┴───────────────────────────────────────────────────────────┘



  Mailbox Assignment Model: The 20 pre-purchased domains are divided: 5 domains (10 mailboxes) dedicated exclusively to Blackink's own self-marketing; 15 domains (30 mailboxes) partitioned across client
  tenants, assigning a dedicated 3-domain / 6-mailbox cluster to each active client. Sending identity and reputation are never pooled across clients.
  Per-Mailbox Pacing & Volume Caps: Mailboxes strictly enforce a daily ceiling of 30–50 cold emails per day, with automated rotation algorithms distributing sequence steps evenly across the client's 6 assigned
  mailboxes.
  Deliverability Sentinel & Auto-Quarantine: Background monitors track bounce rates, spam complaint deltas, and open rate decay per domain. If a client domain records a bounce rate >3% or a spam
  complaint rate >0.08% within a rolling 48-hour window, the Sentinel trips an automatic quarantine: pauses outbound dispatch, replaces the degraded domain with a pre-warmed reserve domain, and posts an alert
  to #blackink-qa .

3.2.4 Client Core Revenue Recipes Initialization
A. The Win-Back Recipe ( 1.8-Rec ) — Ingests the client's historical dead leads, lost owners, and cancelled management agreements. Runs automated skip-tracing and DNC/suppression screening, then dispatches
a 3-touch hyper-personalized re-engagement sequence from the client's own domain: Touch 1 (Market Shift Angle), Touch 2 (Ancillary Value / Ryse Angle), Touch 3 (Direct Check-in). Deploys at a new client in under
2 hours, producing booked appointments within the first 72 hours of tenant launch.
B. The Speed-to-Lead Recipe ( 1.9-Rec ) — Captures incoming owner inquiries from the client's website, listing portals, and paid campaigns, delivering sub-60-second automated responses and instant calendar
booking.

````

<a id="source-b-page-15"></a>

## Source B — Page 15

````text
 SUB-60-SECOND SPEED-TO-LEAD FLOW (DUAL INGESTION PATH)

 ┌───────────────────────────────────────┐             ┌────────────────────────────────────────┐
 │ Path A: Direct Webhook Source         │             │ Path B: Unintegrated Email Notification│
 │ (Website Form, Paid Landing Page)     │             │ (Zillow, Trulia, HotPads, MLS Forms)   │
 └──────────────────┬────────────────────┘             └───────────────────┬────────────────────┘
                    │ Webhook Ingest (<2s)                                 │ Inbound Parse Hook (<5s)
                    ▼                                                      ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Speed-to-Lead Orchestrator (Validates Phone/Email & Checks Non-Poach Gate)                   │
 └──────────────────────────────────────────────┬───────────────────────────────────────────────┘
                                                ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Automated Response Engine (<60 Seconds Total Elapsed Time):                                  │
 │ 1. Triggers Immediate Transactional SMS with 10-min call offer                                │
 │ 2. Dispatches Personalized Confirmation Email with Real-Time Booking Calendar Link           │
 │ 3. Fires High-Priority Alert to Closer Queue in Slack #blackink-setter                       │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘



Email-Parsing Fallback: For legacy listing sources that do not support webhooks, client inquiry notification emails route to a dedicated tenant parse address ( leads@{client-subdomain}.blackink.io ), extracting
prospect name, phone, address, and inquiry text via regex and firing the identical sub-60s workflow.
C. The Async-Close Path ( 1.10-Async ) — Enables small residential owners (1–2 units) to sign standard management agreements electronically. Inbound leads meeting small-portfolio criteria receive an automated
video walkthrough of the management agreement alongside an embedded e-signature link, logging a door_signed event upon document completion without consuming closer calendar slots.

3.2.5 Founding Client Pilot Deployment (September 16–18 Milestone)

 Stage                                              Operational Procedure Executed                                                                              Acceptance Verification Standard

 1. Tenant Cloning                                  Clone Golden Client schema; inject tenant identifiers; provision dedicated 3-domain sending cluster.        Isolated tenant workspace live with zero credential leakage.

 2. Historical Ingest                               Ingest 500+ client dead leads and past owner records via CSV import; execute automated DNC scrub.           Data normalized, deduped, and suppression flags verified.

 3. Compliance Guard                                Execute cross-client non-poach check; verify opt-out tables; validate state-specific calling hours.         Zero overlap with existing books; email-first waterfall holds.

 4. Campaign Launch                                 Arm Win-Back and Speed-to-Lead recipes; dispatch initial batch from client-dedicated mailboxes.             Live outbound emails delivering; tracking webhooks active.

 5. Triage & Routing                                Ingest live prospect replies; execute automated intent classification; post context cards to Slack.         Replies classified correctly; hot leads routed to setter <60s.

 6. Booking & Dashboards                            Complete live meeting booking; verify calendar ICS; update Client Wins Dashboard with real event metrics.   Looker dashboard reflects live appointments and pipeline.


Hands-On Engineering Support: As established in the Engagement Agreement, engineering assistance is explicitly permitted during the September 16–18 pilot. Clients #1–2 are intentionally managed with hands-
on technical guidance to observe friction points, refine database mappings, and harden the operational runbook before enforcing the zero-code standard on September 30.

3.2.6 Week 2 Acceptance Criteria & Definition of Done
The Sprint 2A milestone is officially complete on September 18, 2026, when all of the following technical deliverables are demonstrated live:
1. Automated Reply Classification: Process a test batch of 50 multi-channel replies across all 10 intent classes; verify that the Reply Triage Agent achieves ≥80% classification accuracy and correctly routes high-
   intent leads to Slack with context cards.
2. Deterministic Settlement Execution: Execute a test outcome transaction in Stripe; verify zero dollars charged upfront, successful ACH mandate storage, generation of the timestamped Evidence Packet PDF,
   and creation of the 60-day clawback verification job.
3. Tenant-Isolated Sending Verification: Demonstrate that test campaigns dispatched for Tenant A execute exclusively through Tenant A's dedicated domain cluster, with zero From-Name or domain crossover.
4. Speed-to-Lead Sub-60s Execution: Trigger a test lead via webhook and via the email-parsing address; verify that transactional SMS response, confirmation email, and closer Slack notifications execute in under
   60 seconds.
5. Operational Pilot Tenant: Demonstrate a live, functioning founding client tenant executing active win-back sequences, routing replies into Slack, and displaying real-time metrics in the Client Wins Dashboard.

````

<a id="source-b-page-16"></a>

## Source B — Page 16

````text
Week 3 (Sept 21 – Sept 25, 2026): Sprint 2B — 15-State Portal, Cloner Runbook, Demo Weapons & Preflight
Milestone Standard: Sprint 2B Production Readiness (Full 15-State Client Onboarding Portal, Client Cloner Runbook Path B, Phase 2A Demo Weapons Full Delivery, 72-Hour Launch Preflight Engine, and Operational Control Surfaces)

Week 3 shifts the Blackink platform from a single-tenant deployment into a standardized, repeatable business-in-a-box, focused on: (1) Client Intake — a branded, tokenized 15-state onboarding portal; (2)
Repeatable Tenant Provisioning (Path B) — an operational cloning runbook; (3) Phase 2A Demo Weapons — the full multi-API Rent Analysis Bot, Churn Tripwire, and LLC-to-owner portfolio ingestion; (4) Deterministic
Preflight & Dashboards.

 WEEK 3 COMPLETE ONBOARDING, WEAPONS & GOVERNANCE PIPELINE

                                      [Signed Client Agreement / Contract Closed]
                                                           │
                                          [Automatic Link Generation / Signed URL]
                                                           ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ 15-State Client Checklist Portal (`ADD-9-FULL`)                                                                       │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ States 1–7: Core Setup (Agreement, Auth, PM Profile, Metro Territory, Calendar Slots, Offer Approved)               │
 │ States 8–11: Holistic Value Intake (Historical Dead Leads, Document Upload, Partner Menu, Growth Elections)          │
 │ States 12–15: Technical Launch (Isolated Sending, Lead Routing, Live-Fire Test, Client Preflight Approval)            │
 └─────────────────────────────────────────────────────────┬────────────────────────────────────────────────────────────┘
                                    ┌──────────────────────┴──────────────────────┐
                                    ▼                                             ▼
 ┌────────────────────────────────────────────────────────┐ ┌───────────────────────────────────────────────────────────┐
 │ Path B Client Cloner Runbook (`ADD-1-B`)               │ │ Phase 2A Demo & Retention Weapons                         │
 ├────────────────────────────────────────────────────────┤ ├───────────────────────────────────────────────────────────┤
 │ 1. Clone Golden Client Database & Schema               │ │ 1. Full Rent Analysis Bot: Multi-API Valuation Engine     │
 │ 2. Inject Tenant Profile & PM Software Credentials     │ │ 2. Churn Tripwire: Listing/Deed/Homestead Book Monitor    │
 │ 3. Assign 3-Domain / 6-Mailbox Isolated Cluster        │ │ 3. Owner/Portfolio Feed: Multi-LLC Door Aggregation       │
 │ 4. Auto-Generate First-14-Days Growth Plan             │ └─────────────────────────────┬─────────────────────────────┘
 └──────────────────────────┬─────────────────────────────┘                               │
                            └──────────────────────────────┬──────────────────────────────┘
                                                           ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ 72-Hour Preflight Validator (`PREFLIGHT`) ──► Asserts All 15 Dependencies Green ──► ARMS CAMPAIGNS                  │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ Client View: Client Wins Dashboard (`DASH-WINS`)    │ Internal View: Internal Client Control Center (`OPS-CENTER`)    │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘



3.3.1 The 15-State Client Onboarding Portal ( ADD-9-FULL ) & Holistic Intake
The client onboarding interface replaces manual coordination with a live-status web application accessed via cryptographically signed URLs.

 THE 15-STATE ONBOARDING & HOLISTIC ASSESSMENT FLOW

 [1. Agreement Signed] ──► [2. Payment Auth (Card + ACH)] ──► [3. Contacts Entered] ──► [4. PM Profile (`pm_profile`)]
                                                                                                     │
 [8. Historical Ingest] ◄── [7. Offer / CTA Approved] ◄── [6. Calendar (25 Slots)] ◄── [5. Metro Territory Set]
           │
           ├──► [9. Complete Document Upload ──► Auto-Generates Instant Revenue-Stack Audit Map]
           ├──► [10. Partner Menu Reviewed ──► Checkbox Enrollments Logged for Ancillary Lines]
           └──► [11. Growth & Value Elections ──► Captures New Fees, Streams & Target Services]
                                                               │
 [15. Preflight Green / Live] ◄── [14. Live-Fire Test Passed] ◄── [13. Routing Confirmed] ◄── [12. Compliance / Domains]



Detailed State Specification:
  State 1 — Agreement Signed: Contract execution verified via webhook, storing the countersigned agreement and locking core contractual terms.
  State 2 — Payment Authorization Complete: Zero-deposit payment setup verifying credit card authorization and capturing an ACH direct debit mandate via Stripe Elements.
  State 3 — Primary Client Contacts Entered: Collects and validates contact details for the Managing Broker/Owner and the Operations/Office Lead.
  State 4 — Property Management Profile Completed: Populates the pm_profile schema (specialty tags, asset classes, accepted property types, languages, historical performance metrics).
  State 5 — Metro Territory Established: Defines the contracted geographic territory via zip code arrays or boundary polygons, setting client acceptance criteria and linking the non-poach suppression
  perimeter.
  State 6 — Calendar Connected: Integrates the client's Google Calendar or Calendly workspace, verifying a minimum of 25 open meeting slots across the initial 60-day window.
  State 7 — Client Offer & CTA Approved: Confirms standard messaging hooks, switching incentives, and target customer profiles.
  State 8 — Full Historical Data Ingest (The Win-Back Goldmine): Direct portal upload of historical CRM exports, past owner lists, cancelled management agreements, and dead leads, normalized and
  screened against DNC registries to seed the Win-Back recipe.
  State 9 — Complete Document Upload & Instant Revenue Audit: Client uploads their standard management agreement, master fee schedule, and operational addenda, triggering the ADD-8-Lite templating
  pipeline to generate an immediate, branded Revenue-Stack Audit.

````

<a id="source-b-page-17"></a>

## Source B — Page 17

````text
  State 10 — Partner Menu Selections: Interactive interface presenting pre-negotiated partner lines (pet screening, resident benefits, utility concierge, filter delivery, deposit alternatives, maintenance markup
  policies, eviction protection, Ryse rent advance), establishing the 35% ancillary revenue tracking baseline.
  State 11 — Growth & Value Elections: Structured intake capturing the client's strategic growth goals beyond door count, feeding the 90-day audit cycle and shaping the First-14-Days growth plan.
  State 12 — Sending Identity & Compliance Approved: Assigns dedicated sending domains and numbers; generates bidirectional non-poach suppression lists joining the client's current book into the global
  platform gate.
  State 13 — Campaign & Lead Routing Confirmed: Configures inbound lead-capture webhooks and dedicated email-parsing addresses ( leads@{client-subdomain}.blackink.io ).
  State 14 — Live-Fire Test Passed: Automated end-to-end test verifying that an injected synthetic lead triggers sub-60s notification, schedules a calendar event, and alerts the closer queue in Slack.
  State 15 — Preflight Green & Launch Approved: The preflight validation engine evaluates all prerequisites; once confirmed green, the client clicks "Approve Launch," arming live outbound campaigns.

 -- Schema: src/db/migrations/004_onboarding_states.sql

 CREATE TABLE client_onboarding_states (
     client_id UUID PRIMARY KEY REFERENCES companies(company_id) ON DELETE CASCADE,
     state_1_agreement_signed BOOLEAN DEFAULT FALSE,
     state_2_payment_authorized BOOLEAN DEFAULT FALSE,
     state_3_contacts_entered BOOLEAN DEFAULT FALSE,
     state_4_pm_profile_complete BOOLEAN DEFAULT FALSE,
     state_5_territory_established BOOLEAN DEFAULT FALSE,
     state_6_calendar_connected BOOLEAN DEFAULT FALSE,
     state_7_offer_approved BOOLEAN DEFAULT FALSE,
     state_8_history_imported BOOLEAN DEFAULT FALSE,
     state_9_documents_uploaded BOOLEAN DEFAULT FALSE,
     state_10_partner_menu_reviewed BOOLEAN DEFAULT FALSE,
     state_11_growth_elections_captured BOOLEAN DEFAULT FALSE,
     state_12_compliance_configured BOOLEAN DEFAULT FALSE,
     state_13_routing_confirmed BOOLEAN DEFAULT FALSE,
     state_14_live_fire_passed BOOLEAN DEFAULT FALSE,
     state_15_launch_approved BOOLEAN DEFAULT FALSE,
     current_state_number INTEGER DEFAULT 1,
     portal_access_token VARCHAR(255) UNIQUE NOT NULL,
     portal_token_expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
     completed_at TIMESTAMP WITH TIME ZONE,
     updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );

 CREATE TABLE client_growth_elections (
     election_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
     category VARCHAR(100) NOT NULL, -- 'FEE_MODIFICATION', 'NEW_ANCILLARY', 'SERVICE_EXPANSION'
     item_name VARCHAR(255) NOT NULL,
     selection_status VARCHAR(20) NOT NULL, -- 'YES', 'NO', 'INTERESTED'
     modeled_annual_value_cents BIGINT DEFAULT 0,
     created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );


Auto-Chase Suppression Logic: As the client completes each state in the portal, an event is logged to the shared ledger. The Launch Agent detects completion events in real time and automatically cancels
corresponding chase notifications, preventing redundant follow-up emails.

3.3.2 Client Cloner & Launch OS (Path B Runbook Architecture)
Following the Path B execution model, multi-tenancy is managed via a documented, hardened cloning runbook for Clients #1–2, establishing the blueprint for the automated in-app cloner wizard.

````

<a id="source-b-page-18"></a>

## Source B — Page 18

````text
 PATH B CLIENT CLONING & PROVISIONING ARCHITECTURE

                        ┌──────────────────────────────────────────────┐
                        │      Golden Client Master Template           │
                        │ (Isolated Sandbox Environment Schema)        │
                        └──────────────────────┬───────────────────────┘
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │ Step 1: Database & Schema Provisioning       │
                        │ - Execute `clone_tenant_environment.sh`      │
                        │ - Provision isolated tenant row in Postgres │
                        │ - Enforce tenant isolation via `client_id`   │
                        └──────────────────────┬───────────────────────┘
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │ Step 2: Infrastructure & Deliverability      │
                        │ - Assign 3 Dedicated Domains (6 Mailboxes)   │
                        │ - Connect Instantly Sub-Workspace via API    │
                        │ - Verify SPF/DKIM/DMARC & Warmup Status      │
                        │ - Provision Dedicated Inbound Twilio Number │
                        └──────────────────────┬───────────────────────┘
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │ Step 3: Non-Poach & Compliance Provisioning │
                        │ - Ingest Client Book into Suppression Engine │
                        │ - Generate Bidirectional Cross-Client Gate   │
                        │ - Apply Target Metro Polygon Boundary        │
                        └──────────────────────┬───────────────────────┘
                                               ▼
                        ┌──────────────────────────────────────────────┐
                        │ Step 4: First-14-Days Plan Auto-Generation   │
                        │ - Parse States 8–11 Intake Payloads          │
                        │ - Assemble Engine Activation Schedule        │
                        │ - Render Plan into Client Wins Dashboard     │
                        └──────────────────────────────────────────────┘



1. Database & Tenant Keying: Operator runs clone_tenant_environment.sh passing the client's legal entity name and primary domain. The script provisions a new tenant profile, binds cryptographic API tokens, and
   applies row-level isolation rules.
2. Dedicated Deliverability Cluster Allocation: Allocates a dedicated cluster of 3 sending domains and 6 Google Workspace mailboxes from the pre-warmed pool; provisions a dedicated Twilio 10DLC local phone
   number mapped to the client's operating metro.
3. Suppression & Compliance Initializer: Ingests the client's current owner roster into the non-poach database, establishing cross-suppression rules that work in both directions.
4. First-14-Days Plan Generator: Reviews the client's historical dead-lead count, target metro size, and fee schedule to generate an operational roadmap — Days 1–3: Win-Back activation + Speed-to-Lead
   routing; Days 4–7: Churn Tripwire activation + initial outbound launch; Days 8–14: Ancillary Partner Menu integration + first weekly performance review.
5. Rollback & Circuit Breaker Engine: A documented rollback runbook ( rollback_tenant_provisioning.sh ) revokes API keys, freezes sending queues, decouples suppression joins, and restores the database to its
   pre-clone state if a configuration error occurs.

3.3.3 Phase 2A Demo & Retention Weapons (Full Engine Delivery)

 CHURN TRIPWIRE RETENTION MONITORING ENGINE (`2A-TRIPWIRE`)

                                     [Active Client PM Book Ingested & Synced]
                                                         ▼
                                     ┌───────────────────────────────────────┐
                                     │ Nightly Public Records & Market Sweep │
                                     └───────────────────┬───────────────────┘
          ┌──────────────────────────────────────────────┼──────────────────────────────────────────────┐
          ▼                                              ▼                                              ▼
 [MLS / Public Listing Detector]              [Deed Transfer & Sale Monitor]                 [County Tax Record Auditor]
 - Scans MLS for Active For-Sale Listings     - Ingests County Clerk Deed Recordings         - Detects Homestead Exemption Drops
 - Scans Zillow/Redfin For-Sale-By-Owner      - Identifies Title Transfers / Arm's-Length    - Detects Mailing Address Divergence
 - Detects Price Cuts & Status Changes        - Detects Pre-Foreclosure / Lis Pendens        - Identifies Out-of-State Relocations
          └──────────────────────────────────────────────┼──────────────────────────────────────────────┘
                                                         ▼
                         ┌───────────────────────────────────────────────────────────────┐
                         │ Correlation Engine (Matches Properties to Client Owner Book) │
                         └───────────────────────────────┬───────────────────────────────┘
                                                         ▼
                         ┌───────────────────────────────────────────────────────────────┐
                         │ 1. Generates Real-Time Retention Risk Alert in Slack          │
                         │ 2. Assembles Owner Save Dossier (Property, Signal, Next Step) │
                         │ 3. Logs `door_saved` Opportunity to Events Ledger             │
                         └───────────────────────────────────────────────────────────────┘

````

<a id="source-b-page-19"></a>

## Source B — Page 19

````text
A. Full Rent Analysis Bot ( 2A-BOT-FULL ) — Multi-Source Real-Time Valuation Pipeline integrating live valuation data feeds (RentCast and CoreLogic) via parallel REST API connectors. Confidence Scoring & Range
Math evaluates comparable rental properties within a 1.5-mile radius:
                                                                       Confidence Score = min(100, (Active Comps Count ÷ 10 × 40) + (1 − σrent ÷ μrent) × 60)

Branded PDF Valuation Tear-Sheet: automated headless worker compiles a 1-page property rent report. Twilio Webhook Controller ingests SMS property queries, executes address standardization, queries valuation
APIs, and dispatches SMS replies in under 60 seconds.
B. Churn Tripwire ( 2A-TRIPWIRE ) — Continuously cross-references every property address and owner entity in the client's active management database against daily county public records and market signals.
Signal Detection Classes: MLS Listing Filings, Deed Transfers & Title Changes, Tax & Homestead Exemption Drops, Out-of-State Mailing Address Changes, Management Agreement Anniversary Flags. When a retention
risk is resolved and the owner signs a renewal, the system logs a door_saved event to the ledger.
C. Owner & Portfolio Ingestion Engine ( 2A-FEED-MIN ) — LLC Entity Resolution ingestion pipeline processes purchased portfolio datasets, linking business entities to parent LLCs and individual managing
members. Door Aggregation calculates total residential door counts per owner across Florida counties, identifying high-value targets with 5+ units.

3.3.4 72-Hour Launch Preflight Engine & Health Gates

 -- Schema: src/db/migrations/005_preflight_validator.sql

 CREATE TABLE client_preflight_checks (
     check_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     client_id UUID UNIQUE NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
     check_agreement_executed BOOLEAN DEFAULT FALSE,
     check_stripe_auth_verified BOOLEAN DEFAULT FALSE,
     check_pm_profile_valid BOOLEAN DEFAULT FALSE,
     check_territory_non_empty BOOLEAN DEFAULT FALSE,
     check_calendar_slots_count INTEGER DEFAULT 0,
     check_history_records_count INTEGER DEFAULT 0,
     check_documents_uploaded BOOLEAN DEFAULT FALSE,
     check_partner_menu_completed BOOLEAN DEFAULT FALSE,
     check_domains_spf_dkim_dmarc_valid BOOLEAN DEFAULT FALSE,
     check_non_poach_suppression_active BOOLEAN DEFAULT FALSE,
     check_live_fire_roundtrip_passed BOOLEAN DEFAULT FALSE,
     is_preflight_green BOOLEAN DEFAULT FALSE,
     last_evaluated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );



Gate ID                     Prerequisite Validated                                                                                       Acceptance Standard

 GATE-01                    Stripe Authorization & ACH Mandate                                                                            setup_intent status == 'succeeded'

 GATE-02                    PM Profile Schema Completeness                                                                               All required fields populated

 GATE-03                    Territory Definition & Polygon Bounds                                                                        ≥1 valid ZIP code assigned

 GATE-04                    Calendar Meeting Availability                                                                                ≥25 open slots in next 60 days

 GATE-05                    Historical Data Ingestion                                                                                    ≥50 historical records normalized

 GATE-06                    Document Ingestion & Audit Generation                                                                        Fee schedule uploaded; audit compiled

 GATE-07                    Partner Menu Selections                                                                                      All partner lines reviewed

 GATE-08                    Dedicated Sending Domain Verification                                                                        SPF, DKIM, DMARC 100% valid

 GATE-09                    Bidirectional Non-Poach Suppression                                                                          Client book cross-indexed in gate

 GATE-10                    Live-Fire Roundtrip Verification                                                                             Synthetic lead executes in <60s

 OVERALL STATUS: GREEN (ALL 10 GATES PASS)                                                                                               Enables "Arm Campaigns" control in UI


3.3.5 Operational Dashboards & Control Surfaces
A. Client Wins Dashboard ( DASH-WINS ): Accessible via secure signed URLs, displaying total attended discovery appointments held, verified signed management agreements with door counts, total saved doors
identified by the Churn Tripwire, modeled and collected ancillary revenue, and downloadable Evidence Packet PDFs.
B. Internal Client Control Center ( OPS-CENTER ): Single administrative screen providing visibility across all client tenants — live status of the 15 onboarding states per client, real-time deliverability health, pipeline
volume metrics, and an open exception queue.

````

<a id="source-b-page-20"></a>

## Source B — Page 20

````text
 INTERNAL CLIENT CONTROL CENTER (`OPS-CENTER`) WIREFRAME

 ┌───────────────────┬──────────────┬───────────────┬──────────────────┬─────────────────┬────────────────┬─────────────┐
 │ Tenant Name       │ Launch State │ Preflight     │ Domain Health    │ Active Leads    │ Booked / Held │ Exceptions │
 ├───────────────────┼──────────────┼───────────────┼──────────────────┼─────────────────┼────────────────┼─────────────┤
 │ Suncoast PM       │ State 15/15 │ GREEN          │ 100% (6/6 Warm) │ 412 In-Flight    │ 14 Booked / 11 │ 0 Open      │
 │ Gulf Coast Living │ State 12/15 │ RED (Gate 04) │ 100% (6/6 Warm) │ 0 (Pre-Launch) │ 0 Booked / 0      │ 1 Blocker   │
 │ Tampa Bay Rentals │ State 15/15 │ GREEN          │ 98% (Spam: 0.02%)│ 680 In-Flight   │ 22 Booked / 19 │ 0 Open      │
 │ Orlando Alliance │ State 08/15 │ RED (Gate 05) │ Provisioning       │ 0 (Pre-Launch) │ 0 Booked / 0    │ 1 Blocker   │
 └───────────────────┴──────────────┴───────────────┴──────────────────┴─────────────────┴────────────────┴─────────────┘



3.3.6 Week 3 Acceptance Criteria & Definition of Done
The Sprint 2B milestone is officially complete on September 25, 2026, when all of the following technical deliverables are demonstrated live:
1. Complete 15-State Onboarding Demonstration: Walk through the onboarding portal using a tokenized link; upload sample management agreements and dead leads; select Partner Menu items; confirm
   growth elections; and verify that completion events suppress chase notifications.
2. Instant Revenue Audit Generation: Upload a sample fee schedule in State 9; verify that the engine generates a branded Revenue-Stack Audit PDF displaying fee gaps and partner revenue projections.
3. Path B Cloner Execution: Execute the manual cloning runbook on a test tenant, verifying database creation, tenant parameter injection, isolated domain assignment, and suppression indexing.
4. Full Rent Analysis Bot Live Test: Send property queries to the bot; verify that the system returns rental valuations, confidence scores, and comparable properties in under 60 seconds.
5. Churn Tripwire Alert Verification: Inject a synthetic deed transfer and MLS listing event matching a client property; verify that the system detects the match, posts a retention alert to Slack, and creates a save
   opportunity in the ledger.
6. Preflight Deterministic Gating: Demonstrate that the Preflight engine blocks campaign arming when a prerequisite is missing, and unlocks campaign arming only when all 10 gates evaluate green.
7. Control Surfaces Operational: Verify that the Client Wins Dashboard reflects real-time metrics and that the Internal Control Center accurately displays tenant health, onboarding progress, and exceptions.

````

<a id="source-b-page-21"></a>

## Source B — Page 21

````text
Week 4 (Sept 28 – Sept 30, 2026): Sprint 3 — Verification Engine, Retention Suite, Expansion Registry & Zero-Code Acceptance
Milestone Standard: September 30 Blackink Business-in-a-Box Production Complete (4-Rule Attended Appointment Verification Bar, Automated Dispute Adjudication, Retention Suite, Expansion Offer Registry, Security Baseline, and
Zero-Code Repeatable Tenant Deployment)

Week 4 represents the final production capstone of the September platform build, hardening the operational, verification, and compounding layers of the business across five pillars: the 4-Rule Verification & Dispute
Engine; the Phase 3 Retention Suite; the Expansion Registry & Sub-Engines; the Production Security Baseline ( SEC-BASE ); and the September 30 Production Acceptance Test.

 WEEK 4 COMPLETE VERIFICATION, RETENTION & ACCEPTANCE ARCHITECTURE

 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ The 4-Rule Attended Appointment Verification Engine & Dispute Adjudication                                          │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ Rule 1: Assessor Parcel ID Verified in Polygon │ Rule 2: Live Attended ≥12 Mins (`proof_ref` Log)                 │
 │ Rule 3: Pre-Qualification Documented on Row   │ Rule 4: Matches Client Initialed ICP (Exhibit A)                     │
 │ ──► QA Watchdog Auto-Adjudicates Disputes (Duration + Transcript Ownership Language Check ──► Auto-Deny / Escalate)   │
 └──────────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┘
                                    ┌───────────────────────┴───────────────────────┐
                                    ▼                                               ▼
 ┌────────────────────────────────────────────────────────┐ ┌───────────────────────────────────────────────────────────┐
 │ Phase 3 Retention Suite (Pulled Forward to September) │ │ Expansion Engine & Compounding Sub-Engines                 │
 ├────────────────────────────────────────────────────────┤ ├───────────────────────────────────────────────────────────┤
 │ 1. Retention Guard ($497/mo Book Churn Monitor)        │ │ 1. 17-Row Offer Registry (`entitlement_offers` Table)     │
 │ 2. Rent Gap Report ($197/mo Under-Market Engine)       │ │ 2. One-Click In-App Activation (Flips Entitlement Row)    │
 │ 3. Owner Report Card ($197/mo White-Label PDF)         │ │ 3. Close Detection (Forwarded PM Email Parser)            │
 │ 4. Anniv. Flags & Deed/Listing/Homestead Watch         │ │ 4. Dead-Book Engine, 4-Channel Referrals, Second Pass     │
 └──────────────────────────┬─────────────────────────────┘ └─────────────────────────────┬─────────────────────────────┘
                            └───────────────────────────────┬─────────────────────────────┘
                                                            ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Production Security Baseline (`SEC-BASE`) & Governance Ledgers                                                       │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ - Adversarial Cross-Tenant Leakage Test Suite │ - Automated Daily PostgreSQL Backups + Verified Live Restore         │
 │ - Consent & Channel Eligibility Ledger         │ - Per-Client Variable Cost Ledger (Enrichment, Twilio, AI, Media)   │
 │ - Marketer Content Admin Surface (Safe Staging)│ - Wallet-Capped Paid Growth Modules (Google Search, Meta Harness)   │
 └──────────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┘
                                                            ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ SEPTEMBER 30 ACCEPTANCE TEST: BUSINESS-IN-A-BOX ZERO-CODE REPEATABILITY                                              │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ Golden Client Run ──► Repeat on Tenant #2 via Written Runbook ──► ZERO Code Touched = PRODUCTION COMPLETE            │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘



3.4.1 The 4-Rule Attended Appointment Verification Engine & Dispute Adjudication
Under Blackink's performance-based billing model, revenue is recognized on verified attended appointments. To prevent attribution disputes, every appointment is evaluated programmatically against a 4-rule
qualification bar before an invoice is issued.

````

<a id="source-b-page-22"></a>

## Source B — Page 22

````text
 THE 4-RULE PROGRAMMATIC VERIFICATION BAR

                   [Appointment Completed on Closer / Client Calendar]
                                           ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ RULE 1: Property Ownership & Parcel Verification                                                 │
 │ Assessor Parcel ID (APID) for ≥1 residential unit in contracted polygon verified in database. │
 └─────────────────────────────────────────┬────────────────────────────────────────────────────────┘
                                           │ [PASSED]
 ┌─────────────────────────────────────────▼────────────────────────────────────────────────────────┐
 │ RULE 2: Minimum Attended Duration (≥12 Minutes)                                              │
 │ Both parties present on calendar event for ≥12 minutes; call recording or timestamp stored. │
 └─────────────────────────────────────────┬────────────────────────────────────────────────────────┘
                                           │ [PASSED]
 ┌─────────────────────────────────────────▼────────────────────────────────────────────────────────┐
 │ RULE 3: Documented Pre-Qualification Record                                                      │
 │ Prospect's explicit inbound SMS, email reply, or audit form answer stored on outcome row.        │
 └─────────────────────────────────────────┬────────────────────────────────────────────────────────┘
                                           │ [PASSED]
 ┌─────────────────────────────────────────▼────────────────────────────────────────────────────────┐
 │ RULE 4: Ideal Customer Profile (ICP) Compliance                                                  │
 │ Unit count, property type, and asset class fall within client's initialed Exhibit A agreement.   │
 └─────────────────────────────────────────┬────────────────────────────────────────────────────────┘
                                           │ [ALL 4 PASS]
 ┌─────────────────────────────────────────▼────────────────────────────────────────────────────────┐
 │ STATUS: BILLABLE ATTENDED OUTCOME                                                                │
 │ - Stripe ACH Direct Debit drafted next business day                                              │
 │ - Evidence Packet PDF auto-compiled and emailed with receipt                                     │
 │ - Settlement row updated with timestamp and transaction ID                                       │
 └──────────────────────────────────────────────────────────────────────────────────────────────────┘



Verification Criteria Specification:
  Rule 1 (Assessor Parcel ID Verification): The booking payload must resolve to a verified residential property within the client's contracted geographic territory; the county tax assessor parcel ID (APID) is
  validated against public property feeds before billing is initiated.
  Rule 2 (Live Attendance Duration ≥12 Minutes): Both the property owner and the client representative must remain on the calendar bridge for a minimum of 12 verified minutes, stored in the database as
  proof_ref .

  Rule 3 (Documented Pre-Qualification): The outcome row must contain the prospect's explicit pre-qualification statement captured via SMS, email, or inbound intake prior to the calendar hold.
  Rule 4 (ICP Alignment per Exhibit A): The opportunity must match the property management firm's contracted ICP criteria (residential single-family/small multifamily, minimum rent thresholds, valid
  geographic boundary).
Failure Allocation Rule: If Rule 1, 3, or 4 fails, Blackink absorbs the cost and no charge is generated. A replacement credit is issued only if Rule 2 fails due to a legitimate, documented prospect no-show or early
disconnect.

````

<a id="source-b-page-23"></a>

## Source B — Page 23

````text
 -- Schema: src/db/migrations/006_verification_disputes.sql

 CREATE TABLE appointment_outcomes (
     outcome_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
     contact_id UUID NOT NULL REFERENCES contacts(contact_id),
     appointment_scheduled_at TIMESTAMP WITH TIME ZONE NOT NULL,
     attended_duration_seconds INTEGER NOT NULL DEFAULT 0,
     proof_ref TEXT NOT NULL,
     parcel_id_verified VARCHAR(100) NOT NULL,
     pre_qualification_text TEXT NOT NULL,
     icp_criteria_passed BOOLEAN DEFAULT TRUE,
     is_billable BOOLEAN DEFAULT FALSE,
     billing_status VARCHAR(50) DEFAULT 'PENDING',
     dispute_state VARCHAR(50) DEFAULT 'NONE',
     created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );

 CREATE TABLE dispute_adjudications (
     adjudication_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     outcome_id UUID UNIQUE NOT NULL REFERENCES appointment_outcomes(outcome_id) ON DELETE CASCADE,
     client_dispute_reason TEXT NOT NULL,
     qa_agent_duration_check BOOLEAN NOT NULL,
     qa_agent_transcript_keyword_match BOOLEAN NOT NULL,
     adjudication_verdict VARCHAR(50) NOT NULL, -- 'AUTO_DENY', 'AUTO_CREDIT', 'ESCALATE_TO_FOUNDER'
     verdict_explanation TEXT NOT NULL,
     adjudicated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );


Dispute Adjudication Workflow: Clients have a 5-business-day window to flag an appointment outcome. The QA Watchdog Agent auto-adjudicates claims: (1) Duration Verification — checks proof_ref duration
log; (2) Transcript Linguistic Evaluation — evaluates whether property ownership, unit counts, or management needs were discussed; (3) Deterministic Verdict — if duration was ≥12 minutes AND transcript contains
verified ownership context, the dispute is Auto-Denied with an evidence summary; if duration was <12 minutes due to a legitimate prospect departure, a replacement credit is issued automatically (1 free
replacement per 4 billables, max 4/month).
Retention Floor Rule: If rolling 90-day signed-to-attended conversion is ≥15%, no goodwill credits are owed. If conversion drops below 8% for two consecutive months, either party may terminate without penalty.

3.4.2 Phase 3 Retention Suite (Pulled Forward to September)
Acquiring a new property management client costs substantial capital, but firms lose 15–25% of their doors annually to preventable owner churn. Week 4 deploys three automated recurring products pointed inward
at clients' existing portfolios.

 PHASE 3 RETENTION SUITE ARCHITECTURE

                                      [Client's Active Managed Doors Synchronized]
                                                           ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Retention Guard Engine ($497/month Subscription)                                                                     │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ Scans client's active owner book nightly across 5 risk vectors:                                                      │
 │ 1. MLS & FSBO Sale Listings: Detects for-sale listings before the manager is notified                              │
 │ 2. County Deed Transfers: Flags quitclaims, warranty deeds, or title transfers                                       │
 │ 3. Homestead Exemption Filings: Identifies rentals transitioning to primary residences                               │
 │ 4. Out-of-State Tax Address Changes: Flags relocation and estate transitions                                         │
 │ 5. Agreement Anniversary Triggers: Prepares renewal campaigns 60 days prior to expiry                                │
 └─────────────────────────────────────────────────────────┬────────────────────────────────────────────────────────────┘
                                    ┌──────────────────────┴──────────────────────┐
                                    ▼                                             ▼
 ┌────────────────────────────────────────────────────────┐ ┌───────────────────────────────────────────────────────────┐
 │ Rent Gap Report ($197/month Engine)                    │ │ Owner Report Card ($197/month Retention Product)          │
 ├────────────────────────────────────────────────────────┤ ├───────────────────────────────────────────────────────────┤
 │ - Analyzes rent rolls against current market comps     │ │ - Automated quarterly branded PDF generated per owner     │
 │ - Identifies units renting ≥10% under market       │ │ - Displays property equity gains, local market yield,     │
 │ - Arms manager with rent increase justification data   │ │   maintenance summaries, and portfolio performance metrics│
 │ - Drives client management fee growth & owner loyalty │ │ - Makes independent PMs look institutional to owners       │
 └────────────────────────────────────────────────────────┘ └───────────────────────────────────────────────────────────┘



Retention Guard ( retention_guard — $497/mo): Executes nightly public records and market scans across every property in the client's database, flagging sell signals, price drops, and listing filings before the
property leaves the book. Preserving 20 doors annually on a 400-door portfolio saves ~$43,000 in management revenue, delivering a ~7x ROI.
Rent Gap Report ( rent_gap_report — $197/mo): Ingests active lease rates and compares them against real-time hyper-local market comps, compiling a rent-increase brief that proportionally increases the
manager's 8–10% management fee.

````

<a id="source-b-page-24"></a>

## Source B — Page 24

````text
Owner Report Card ( owner_report_card — $197/mo): Generates white-labeled quarterly property performance reports for each property owner, serving as a low-cost, institutional retention tool.

3.4.3 Expansion Engine, Offer Registry & Sub-Engines
Blackink enforces a strict architectural invariant: every commercial product, tier, add-on, seat, and fee exists as a database row in the entitlement_offers table. No pricing logic is compiled into agents.

 -- Schema: src/db/migrations/007_expansion_registry.sql

 CREATE TABLE entitlement_offers (
     offer_id VARCHAR(100) PRIMARY KEY,
     display_name VARCHAR(255) NOT NULL,
     price_cents BIGINT NOT NULL,
     billing_model VARCHAR(50) NOT NULL, -- 'FREE', 'SUBSCRIPTION_MONTHLY', 'METERED_EVENT', 'UPFRONT_PACK', 'REVENUE_SHARE'
     stripe_price_id VARCHAR(100) NOT NULL,
     trigger_condition VARCHAR(100) NOT NULL,
     eligibility_predicate TEXT NOT NULL,
     cooldown_days INTEGER DEFAULT 30,
     monthly_cap INTEGER,
     is_enabled BOOLEAN DEFAULT TRUE,
     created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );

 CREATE TABLE client_active_entitlements (
     entitlement_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
     offer_id VARCHAR(100) NOT NULL REFERENCES entitlement_offers(offer_id),
     status VARCHAR(50) DEFAULT 'ACTIVE',
     activated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
     expires_at TIMESTAMP WITH TIME ZONE
 );


The 17 Day-One Offer Rows (Seeded in PostgreSQL):

Row ID                               Price / Model                  Trigger Condition                                          Eligibility Predicate

 county_rank                          $0 (Free)                     Firm appears in sweep                                      Always eligible

 first_appointment_free               $0 (Free)                     30+ days on free tier                                      1 per firm lifetime; max 5/county/mo

 appt_owner_1_4                       $175 / event                  Attended meeting (1–4 doors)                               Passes 4-rule verification bar

 appt_owner_5_9                       $350 / event                  Attended meeting (5–9 doors)                               Passes 4-rule verification bar

 appt_owner_10plus                    $500 / event                  Attended meeting (10+ doors)                               Passes 4-rule verification bar

 appt_seat_a_rate                     $125 / event                  Attended meeting (Seat A holder)                           Client holds active Seat A

 appt_pack_8                          $1,200 pack                   Upfront purchase                                           Account active; drawn down per show

 appt_standing_order                  $320 / month                  Pack drawn down in <3 weeks                                Subscription; includes 2 appts/month

 appt_principal                       $750 / event                  Attended meeting (20–80 doors)                             Passes 4-rule bar; principal verified

 appt_second_pass                     $75 / event                   Pre-qualified lead ≥90d prior                              Transferable consent verified in DB

 appt_deadbook                        $75 / event                   Sourced from client dead-book                              Passes 4-rule bar; dead-book source

 respond                              $249 / month                  Inbound response tier                                      Inbound concierge active

 retention_guard                      $497 / month                  Client book churn monitoring                               Client PM book uploaded

 growth_os                            $897 / $997 mo                Door Growth Operating System                               All county engines active

 large_book_band                      $1.50–$2.50/door              Client book ≥400 doors                                     Replaces Growth OS; metered monthly

 rent_gap_report                      $197 / month                  ≥5 units renting under market                              Rent roll uploaded

 owner_report_card                    $197 / month                  Client active ≥60 days                                     Quarterly reporting enabled

 deadbook_engine                      $99/mo + $75/appt             Free pass produced ≥1 show                                 Dead-book records available

 county_additional                    $397 / month                  ≥10 doors in adjacent county                               Target county active

 vendor_intro                         $50 / door                    Partner Menu lines active                                  Earned in 4 tranches ($20/$10/$10/$10)

 seat_a_door_gen                      $999 → $1,999/mo              First refusal on county leads                              1 per county; escalates at 26 appts/60d

````

<a id="source-b-page-25"></a>

## Source B — Page 25

````text
 seat_b_comp_intel                        $999 / month                      Competitor distress watch                                               1 per county; displacement active

 seat_both                                $1,799 → $2,799/mo                Dual county exclusivity                                                 Combines Seat A and Seat B

 seat_adjacent                            $799 / month                      Right of first refusal on next county                                   Adjacent market opening


Compounding Sub-Engines:
   One-Click In-App Activation: When a client approves an expansion recommendation, the system executes an atomic transaction flipping the entitlement row in PostgreSQL and updating Stripe billing.
   Close Detection Email Parser: Ingests forwarded confirmation emails from client PM software, extracts owner and property data via regex, and sets signed=TRUE on the corresponding outcome row
   automatically.
   Dead-Book Reactivation Engine ( deadbook_engine ): Connects to client historical dead-lead lists, providing free initial ingestion and running compliant outreach from the client's own domain, billed at $99/month
   plus $75 per attended appointment.
   4-Channel Referral Credit Ledger: Client-to-Client Referral ($250 credit at first paid month); New County Referral ($500 credit); Free-Tier Referral (1 free appointment credit); Vendor Introductions (reciprocal
   revenue-share credits); Ask Trigger fires automatically upon signed=TRUE detection.
   Second Pass & Unsold Routing: Where an attended appointment does not convert within 90 days, the pre-qualified owner routes into the Second Pass pool ($75/appointment) with transferability consent
   verified, unless still in the original client's active suppression file.
   Two-Seat County Exclusivity & First-Refusal Timers: Supports seat_a_holder (exclusive door generation) and seat_b_holder (competitor intelligence) per county. County appointments are held exclusively
   for the Seat A holder until 9:00 AM the next business day. If Seat A and Seat B belong to competing firms, Seat B displacement campaigns automatically suppress Seat A's active client book.

3.4.4 Production Security Baseline ( SEC-BASE ) & Governance Ledgers

 SECURITY, ISOLATION & GOVERNANCE ARCHITECTURE

 ┌───────────────────────────────────────┐             ┌────────────────────────────────────────┐
 │ Consent & Channel Eligibility Ledger │              │ Per-Client Cost & Margin Ledger        │
 │ (`CONSENT-LEDGER`)                    │             │ (`COST-LEDGER`)                        │
 ├───────────────────────────────────────┤             ├────────────────────────────────────────┤
 │ - Durable per-contact audit trail     │             │ - Variable cost tracking per tenant:   │
 │ - Exact consent capture timestamps    │             │   Enrichment, Twilio SMS/Voice,        │
 │ - Source list attribution & channel   │             │   Instantly, AI tokens, Paid spend     │
 │ - Verified opt-out expiration dates   │             │ - True contribution margin calculated │
 └───────────────────┬───────────────────┘             └───────────────────┬────────────────────┘
                     └──────────────────────────┬───────────────────────────┘
                                                ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Production Security Baseline (`SEC-BASE`) Infrastructure                                     │
 ├──────────────────────────────────────────────────────────────────────────────────────────────┤
 │ 1. Automated Cross-Tenant Leakage Test Suite (Validates zero cross-client record queries)    │
 │ 2. Role-Based Access Control (RBAC) & Encrypted Secrets Storage                              │
 │ 3. Automated Daily PostgreSQL Backups with Demonstrated Live Restore Test in Staging         │
 │ 4. Marketer Content Admin Surface: Safe template staging & non-technical copy management     │
 │ 5. Wallet-Capped Paid Growth: Hard budget limits ($150–$300 initial risk) on Google & Meta   │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘



A. Cross-Tenant Leakage Test Suite: An automated adversarial test suite executes nightly in CI/CD, injecting synthetic tenant records across multiple counties and simulating cross-tenant database lookups,
vector retrievals, and campaign dispatches. Asserts that database queries partition strictly by client_id , failing the build if any cross-client data leakage is detected.
B. Automated Backups & Disaster Recovery Verification: Configures automated daily PostgreSQL snapshots with point-in-time recovery (PITR). A full database restore is executed in an isolated staging
environment, demonstrating recovery of all tables, indexes, and event streams without data loss.
C. Governance & Cost Ledgers: Consent & Channel Eligibility Ledger ( CONSENT-LEDGER ) is an immutable datastore for compliance inquiries; Per-Client Cost Ledger ( COST-LEDGER ) ingests variable operational costs to
render gross revenue alongside net contribution margin; Marketer Content Admin Surface ( CONTENT-ADMIN ) allows non-technical marketing personnel to update copy safely; Wallet-Capped Paid Growth configures
Google Search and retargeting sync under strict database wallet caps ($150–$300 initial risk budget per client).

3.4.5 The September 30 Production Complete Acceptance Test
The ultimate definition of done is the September 30 Business-in-a-Box Repeatability Test. The entire platform lifecycle must execute across two distinct environments with zero code modifications.

Stage                                     Operational Procedure Executed                                                                                     Acceptance Standard

 1. Golden Client Demonstration           Run complete lifecycle: Clone → Onboard → Launch → Service → Measure → Bill with Evidence Packet.                  Demonstrated live on video call; all event records valid in DB.

 2. Second Tenant Repeat Test             Execute Path B Cloner Runbook on a fresh test tenant; import dead leads, verify preflight, launch, and bill.       Completed by an operator following written runbook with ZERO code edits.

 3. Verification & Settlement             Inject attended appointment; verify 4-rule evaluation, Stripe payment intent, and dynamic Evidence PDF             Settlement row created; ACH drafted; evidence PDF attached.
                                          generation.

 4. Auto-Dispute Adjudication             Submit test dispute; verify QA Watchdog evaluates duration and transcript, auto-denying or crediting correctly.    Verdict rendered programmatically in <30 seconds.

````

<a id="source-b-page-26"></a>

## Source B — Page 26

````text
 5. Retention Guard Alert          Trigger synthetic deed transfer on client book; verify Retention Guard detects risk and logs save opportunity.   Alert rendered in Slack; door_saved logged to ledger.

 6. Security & Restore QA          Execute CI/CD cross-tenant leakage test suite and show successful database restore in staging environment.       All assertion tests pass green; zero data leakage verified.

 FINAL VERDICT: PRODUCTION COMPLETE — Full 600-Hour Platform Operational


3.4.6 Week 4 Acceptance Criteria & Final Definition of Done
The Blackink platform achieves final production completion on September 30, 2026, when all of the following deliverables are demonstrated live:
1. 4-Rule Verification & Dispute Adjudication: Demonstrate automated qualification of an attended appointment against parcel validation, duration logs (≥12 mins), pre-qualification records, and ICP criteria.
   Submit a synthetic dispute and confirm the QA Watchdog Agent renders a deterministic verdict.
2. Phase 3 Retention Suite Live: Demonstrate that Retention Guard detects listing and deed changes, Rent Gap Report identifies under-market units, and Owner Report Card compiles a branded quarterly PDF.
3. Expansion Offer Registry: Demonstrate one-click in-app activation of an add-on offer from the 17-row database registry, confirming atomic entitlement updates and Stripe subscription changes.
4. Sub-Engines Operational: Verify that the close-detection email parser marks signed=TRUE , dead-book reactivation runs on client domains, 4-channel referral credits update the ledger, Second Pass routes 90-
   day unclosed leads, and Seat A holds leads until 9:00 AM the next business day.
5. Security Baseline & Live Restore: Verify passing test results on the cross-tenant leakage test suite, confirm Consent and Cost Ledger records, and demonstrate a successful database backup and live restore in
   staging.
6. Two-Tenant Zero-Code Repeatability: Successfully execute the full business-in-a-box lifecycle on the Golden Client, followed by an immediate, clean execution on a second test tenant via the written runbook
   without touching a single line of application code.

````

<a id="source-b-page-27"></a>

## Source B — Page 27

````text
4. Commercial Offer & Database Row Matrix

All commercial terms exist strictly as database rows in the offer registry with Stripe Price IDs.

Row Key                             Display Name                                                    Commercial Model   Trigger & Verification Rule

 county_rank                         Speed & Market Rank Report                                     Free               PM appears in county sweep; monthly report emailed.

 first_appointment_free              First Attended Meeting Credit                                  Free               30+ days on free tier (1/firm lifetime, 5/county/mo max).

 appt_owner_1_4                      Attended Appt (1–4 Doors)                                      Metered Event      Verified meeting all 4 verification rules (1–4 doors).

 appt_owner_5_9                      Attended Appt (5–9 Doors)                                      Metered Event      Verified meeting all 4 verification rules (5–9 doors).

 appt_owner_10plus                   Attended Appt (10+ Doors)                                      Metered Event      Verified meeting all 4 verification rules (10+ doors).

 appt_seat_a_rate                    Seat A Exclusive Appt Rate                                     Metered Event      Discounted rate for county Seat A holder.

 appt_pack_8                         8-Appt Bulk Drawdown Pack                                      Upfront Pack       Upfront purchase; drawn down per verified attended meeting.

 appt_standing_order                 Monthly Appt Standing Order                                    Subscription       Includes 2 attended appointments/month.

 appt_principal                      Large Operator / Principal                                     Metered Event      Attended meeting with 20–80 door portfolio operator.

 appt_second_pass                    Second Pass Re-Offer Lead                                      Metered Event      Pre-qualified owner ≥90 days prior, transferable consent verified.

 appt_deadbook                       Dead-Book Reactivated Appt                                     Metered Event      Sourced from client historical dead-book ingest.

 respond                             Inbound Speed-to-Lead Tier                                     Subscription       Lead Agent answering inquiries <5m 24/7 (3 engines).

 retention_guard                     Client Book Churn Monitor                                      Subscription       Nightly sweep on client's own owners for listing/deed changes.

 growth_os                           Door Growth Operating System                                   Subscription       All county engines, referral desk, mail files, owner reporting.

 large_book_band                     Scaled Enterprise Tier                                         Subscription       Metered monthly by active door count (≥400 doors).

 rent_gap_report                     Under-Market Rent Report                                       Subscription       Below-market engine identifies ≥5 under-rented units.

 owner_report_card                   Quarterly Owner PDF Report                                     Subscription       Client active 60+ days; white-label retention reports.

 deadbook_engine                     Dead-Book Campaign Suite                                       Sub + Event        Ongoing dead-book monitoring on client domains.

 seat_a_door_gen                     Exclusive Door Gen Seat                                        Subscription       First refusal on county leads held to 9 AM next business day.

 seat_b_comp_intel                   Competitor Intelligence Seat                                   Subscription       Competitor distress watch and displacement campaigns.

 seat_both                           Combined Dual-Seat License                                     Subscription       Complete county exclusivity across Seats A and B.

 vendor_intro                        Partner Menu Monetization                                      Revenue Share      Earned in 4 tranches: billable, 90d, 180d, 365d.

````

<a id="source-b-page-28"></a>

## Source B — Page 28

````text
5. Blackink Governed Nine-Agent Workforce Architecture

System Specification: Shared Agent Core, Specialist Agent Profiles & Autonomous Operational Framework

5.1 The Agent Operating System (AOS) & Shared Core Architecture
Blackink is architected not as a loose collection of disconnected chatbots, but as a unified, deterministic Agent Operating System (AOS). The human operator interacts with the platform through a centralized Slack
cockpit, while the underlying events stream and outcomes ledger serve as the shared single source of truth. Deterministic code strictly governs money, legal obligations, compliance rules, and hard safety gates. The
nine specialist agents operate within bounded domains—managing language generation, intent prioritization, market intelligence analysis, and recommendation drafting. Autonomous execution is only unlocked after
an agent earns authority through verified accuracy.

 BLACKINK AGENT OPERATING SYSTEM (AOS) ARCHITECTURAL TOPOLOGY

                                       ┌──────────────────────────────┐
                                       │      Slack Agent Hub         │
                                       │   (@Blackink Central Router) │
                                       └──────────────┬───────────────┘
                                                      ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ SHARED AGENT CORE (CHASSIS)                                                                                          │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ • Tenant Isolation (`client_id` Scoping)          • Payload-Bound Cryptographic Hash Approvals (`SHA-256`)           │
 │ • Distributed Leases & Cross-Agent Locking        • Centralized Memory Spine & Context Store                         │
 │ • Idempotency Keys & Retry State Machines         • Observability, Heartbeats & Circuit Breakers                     │
 └────────────────────────────────────────────────────┬─────────────────────────────────────────────────────────────────┘
          ┌───────────────────────────────────────────┼───────────────────────────────────────────┐
          ▼                                           ▼                                           ▼
 ┌─────────────────────────────────┐ ┌─────────────────────────────────┐ ┌─────────────────────────────────┐
 │ FRONT-OFFICE AGENTS             │ │ MID-OFFICE AGENTS               │ │ BACK-OFFICE & GOVERNANCE AGENTS │
 ├─────────────────────────────────┤ ├─────────────────────────────────┤ ├─────────────────────────────────┤
 │ 1. Prospecting Agent (Hunter)   │ │ 4. Lead Agent (Reply Triage)    │ │ 7. Economics & Growth (Vera)    │
 │ 2. Campaign Agent (Cora/Relay) │ │ 5. Reactivation & Nurture Agent │ │ 8. QA / Watchdog Agent           │
 │ 3. Setter Copilot               │ │ 6. Referral Agent               │ │ 9. Launch Agent (Cloner/Launch) │
 └─────────────────────────────────┘ └─────────────────────────────────┘ └─────────────────────────────────┘
                                                      ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ DETERMINISTIC PLATFORM GATES (UNBYPASSABLE HARD STOPS — NO LLM ACCESS)                                              │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ • Non-Poach Cross-Client Suppression Engine       • National/State DNC Linter & Quiet-Hours Filter                   │
 │ • Stripe Billing & Settlement State Machine       • Consent & Channel Eligibility Ledger                             │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘



The Shared Agent Core (Chassis)
The Shared Agent Core provides the underlying scaffolding upon which all nine specialist agents execute. Agents are implemented as declarative policies running on top of this shared chassis rather than standalone
codebases.

Subsystem                                      Technical Implementation & Invariant Rule

 Tenant Isolation                              Every database row, Redis key, cache entry, vector embedding, and audit record is strictly partitioned by client_id . CI/CD executes adversarial cross-tenant leakage tests nightly.

 Payload-Bound Approvals                       Every Slack approval button ( Approve , Revise , Reject ) is cryptographically bound to a SHA-256 hash of the exact message body, recipient ID, and configuration state. An action cannot execute under
                                               an outdated click if the underlying payload has changed.

 Cross-Agent Object Locks                      Distributed Redis leases prevent race conditions. If the Campaign Agent holds an active lock on an opportunity, the Reactivation Agent cannot initiate contradictory outreach.

 Idempotency Engine                            Every outbound send, calendar booking, database update, and billing trigger requires a unique idempotency key ( idempotency_key = hash(tenant_id + entity_id + action_type +
                                               timestamp_bucket) ) to prevent duplicate sends or double charges.

 Persistent Circuit Breaker                    Emergency halt controls at the global, tenant, campaign, or channel level freeze schedulers indefinitely until an authorized human issues a resume command in Slack.


Autonomy Ladder & Governance Bands
Blackink enforces a strict governance model where autonomy is earned at the individual action-class level rather than granted globally across an entire agent. Any critical failure, data discrepancy, or compliance
incident immediately demotes that action class back to mandatory human review.

Authority Band                            Operational Capability                                                            Promotion Requirement                                            Demotion Trigger

 Band 1: Observe & Report                 Agent reads, analyzes, drafts, and recommends. Zero autonomous external           Default baseline for all new action classes.                      Immediate on any critical runtime fault.
                                          actions permitted.

 Band 2: Class Approval (One-Tap)         Concrete action card queued in Slack for one-click human authorization            50 consecutive clean human approvals (≥95% approval rate).        ≥1 policy violation or approval rejection spike (>5%).
                                          (Approve/Reject).

````

<a id="source-b-page-29"></a>

## Source B — Page 29

````text
Band 3: Bounded Auto-Execution   Reversible, low-risk actions execute autonomously within hard-coded rate,         250+ clean executions with <2% dispute/reversal rate and           Any customer dispute, unhandled error, or reversal incident.
                                 send, and spend caps.                                                             passing eval tests.

NEVER AUTONOMOUS                 Permanent human-only gate: pricing terms, billing charges, refunds, legal         No promotion path exists. Hardcoded invariant in deterministic     N/A (Permanent Architectural Boundary)
                                 replies, and contract closing.                                                    code.


Central Slack Hub Cockpit Topology

Slack Channel                        Primary Operational Purpose & Surface Interaction

 #blackink-command                    Executive operating cockpit: macro quota queries, portfolio health summaries, emergency global pause/resume controls, and cross-client system alerts.

 #blackink-setter                     Human conversation cockpit: 1-screen context cards for inbound hot leads, suggested openers, objection battle-cards, and manual dial tasks.

 #sales-replies                       Real-time inbound communication stream: ingests prospect emails and SMS replies with intent classification tags and one-tap triage action buttons.

 #dial-tasks                          Prioritized daily calling queue: populates phone tasks with direct dial lines, timezone calculations, and Sendspark video engagement metrics.

 #client-{name}-growth                Dedicated tenant growth workspace: daily quota pacing, active campaign stats, hot leads, test proposals, and referral desk updates.


 #client-{name}-launch                Dedicated onboarding channel: real-time 15-state checklist tracking, missing asset alerts, preflight status, and launch approvals.

 #blackink-qa                         Health and resilience channel: deliverability alerts, domain reputation drops, dead-letter queues, failed webhooks, and automated cross-tenant leakage test reports.

 #blackink-economics                  Financial performance channel: per-tenant cost ledgers, contribution margins, paid wallet consumption, Economics Governor flags, and books-ready exports.

````

<a id="source-b-page-30"></a>

## Source B — Page 30

````text
5.2 Comprehensive Specialist Agent Profiles
Agent Name                                Core Engine Ancestry                                          Primary Operational Mission

 1. Launch Agent                          Launch OS / Golden Cloner                                     Rapid onboarding, preflight validation & governed launch.

 2. Prospecting Agent                     Hunter / Scout                                                Entity resolution, multi-LLC aggregation & owner scoring.

 3. Campaign Agent                        Cora (Draft) + Relay (Execute)                                Multi-touch sequencing, audit generation & bandit testing.

 4. Lead Agent                            Reply Triage Agent                                            <5min inbound triage, context assembly & weekend mode.

 5. Reactivation & Nurture                Reactivation Engine                                           Dead-lead revival, timing memory & churn save workflows.

 6. Referral Agent                        Referral & Partner Desk                                       B2B partner ecosystems, Realtor pipelines & Ryse attach.

 7. Economics & Growth                    Vera                                                          Financial reconciliation, cost ledger & governor bands.

 8. QA / Watchdog Agent                   Watchdog Sentinel                                             Tenancy isolation, deliverability health & self-healing.

 9. Setter Copilot                        Closer Cockpit                                                1-screen call context cards, opener logic & rubric scoring.


1. Launch Agent
Mission: Transform a newly signed property management contract into a fully configured, compliant, and active client tenant within 72 hours of receiving required data, while managing clean offboarding if a client
churns.

 LAUNCH AGENT WORKFLOW & STATE MACHINE

 [Agreement Signed Webhook] ──► [Generate Tokenized Onboarding Portal Link]
                                                ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ 15-State Intake Monitoring & Auto-Chase Subsystem                                                                    │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ • Tracks States 1–7 (Core Profile, Payment Auth, Calendar Slots, Offer Approved)                                    │
 │ • Tracks States 8–11 (Historical CRM Ingest, Document Upload, Partner Menu, Growth Elections)                       │
 │ • Ingested items fire events to Ledger ──► Auto-chase engine immediately cancels matching reminder notifications    │
 └──────────────────────────────────────────────┬───────────────────────────────────────────────────────────────────────┘
                                                ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Automated Tenant Provisioning Pipeline                                                                               │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ 1. Clones Golden Client master schema; isolates tenant via unique `client_id`                                       │
 │ 2. Provisions 3-domain / 6-mailbox dedicated sending cluster with validated SPF/DKIM/DMARC                          │
 │ 3. Ingests client active book into non-poach suppression database                                                   │
 │ 4. Parses fee schedule & historical data ──► Compiles First-14-Days Operational Growth Plan                         │
 └──────────────────────────────────────────────┬───────────────────────────────────────────────────────────────────────┘
                                                ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Preflight Red-Team Verification (Asserts All 10 Core Gates Green) ──► Arms Campaigns                                │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘



Core Responsibilities: Coordinates the 15-state onboarding checklist as a live state machine; clones the Golden Client database template, injecting tenant configuration parameters and isolating tenant credentials;
assigns pre-warmed sending domains, Instantly sub-workspaces, and dedicated local Twilio phone numbers; evaluates preflight checks; auto-generates the customized First-14-Days growth plan; executes governed
offboarding teardowns.
Input Interfaces: Signed contract webhooks, Stripe payment intents, uploaded CSV spreadsheets, fee schedule documents, and calendar credentials. Output Interfaces: Provisioned PostgreSQL schemas,
tokenized portal links, First-14-Days plans, and Slack launch status cards in #client-launch . Integrated Tools: Database cloner scripts, DNS validation tools, Twilio sub-account APIs, and Google Calendar connection
validators.
Autonomy Bounds & Safety Stops: Band 1 (Default) observes checklist progress and drafts First-14-Days plan; Band 2 (One-Tap) queues campaign arming in Slack once preflight is 100% green; Hard Stop —
cannot launch campaigns if any preflight check evaluates red or payment authorization is unverified.

2. Prospecting Agent (Hunter / Scout)
Mission: Maintain an unexhausted pipeline of qualified property owners, principals, and acquisition opportunities without exhausting market territory or violating compliance boundaries.

````

<a id="source-b-page-31"></a>

## Source B — Page 31

````text
 PROSPECTING AGENT RESOLUTION & SCORING PIPELINE

 [Raw County Tax / Deed Feeds] ──► [LLC Entity Resolution Engine] ──► [Cross-County Owner Aggregation]
                                                                                    ▼
                                                                    [Maps Real Individuals across LLCs]
                                                                                    ▼
 [Ranked Top-40 Metro Artifact] ◄── [Deterministic Scoring Algorithm] ◄── [Calculates Total Portfolio Doors]
                ▼
 [Pre-Send Non-Poach Screening] ──► [Verified Contacts Enriched] ──► [Dispatched to Campaign Queue]



Core Responsibilities: Ingests public county deed recordings, tax assessor rolls, and DBPR license registries across target Florida metros; resolves corporate LLC owners back to true individual managing members
and beneficial owners; calculates the Owner Score:
                                                          Owner Score = (Verified Doors × 15) + (Distress Multiplier × 20) + (In-Market Proximity × 10) − (Entity Fragmentation Penalty)

Monitors pipeline inventory against active client 4-week quotas; operates targeted acquisition sub-recipes: Portfolio Intercept (5+ units), Retiring-Broker Radar, Absentee Owners, Eviction Dockets, and Review-Mining
switchers.
Input Interfaces: County property data feeds, DBPR license files, Google Maps sweeps, skip-trace APIs, and active client suppression lists. Output Interfaces: Verified companies and contacts database records,
ranked top-40 metro owner artifacts, and source quality reports. Integrated Tools: Standalone Hunter resolution droplet, Tracerfy skip-trace APIs, county clerk scraper adapters, address standardization engines.
Autonomy Bounds & Safety Stops: Band 1 extracts data, scores entities, and drafts candidate queues; Band 2 enqueues top-decile verified owner batches for campaign ingestion; Hard Stop — hard-blocked from
querying or targeting any entity present on active client suppression or non-poach lists.

3. Campaign Agent (Cora & Relay)
Mission: Coordinate, execute, and dynamically optimize multi-touch, multi-channel growth campaigns across email, human phone prompts, LinkedIn, and transactional SMS without requiring human copy rewrites
for every touch.

 CAMPAIGN AGENT (CORA + RELAY) DISPATCH ENGINE

 ┌────────────────────────────────────────────────────────┐ ┌───────────────────────────────────────────────────────────┐
 │ Cora (Language, Personalization & Optimization)        │ │ Relay (Execution, Idempotency & Rate Throttling)          │
 ├────────────────────────────────────────────────────────┤ ├───────────────────────────────────────────────────────────┤
 │ • Ingests prospect profile + ghost-shopper audit score │ │ • Enforces per-mailbox rate limits (30–50 sends/day)      │
 │ • Drafts dynamic 5-touch sequenced payloads            │ │ • Injects payload-bound SHA-256 idempotency keys           │
 │ • Injects Sendspark dynamic video parameters           │ │ • Manages cell-allocation rotation across active domains │
 │ • Formulates weekly copy/offer test proposals          │ │ • Halts dispatch immediately on circuit-breaker trip      │
 └──────────────────────────┬─────────────────────────────┘ └─────────────────────────────┬─────────────────────────────┘
                            └───────────────────────────────┬─────────────────────────────┘
                                                            ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Deterministic Send Gate: Lints DNC, Validates Opt-Outs & Enforces Cold Email-First Waterfall                        │
 └──────────────────────────────────────────────────────────┬───────────────────────────────────────────────────────────┘
                                                            ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Outbound Dispatch ──► Dispatches Email ──► Logs `touch_sent` Event to Shared Ledger                                 │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘



Core Responsibilities: Generates hyper-personalized 5-touch outbound sequences combining ghost-shopper response metrics, revenue loss models, and fee-stack proofs; embeds Sendspark dynamic video landing
pages into cold email payloads; operates the public Rent Analysis Bot campaign trigger and self-serve audit funnel; coordinates Relay execution adapters to ensure daily mailbox sending limits and domain pacing
rules are maintained; formulates weekly test proposals using contextual bandit algorithms.
Input Interfaces: Enriched contact records, ghost-shopper latency logs, Sendspark video IDs, and real-time open/click engagement webhooks. Output Interfaces: Outbound emails via Instantly APIs, phone tasks
in #dial-tasks , and weekly optimization digests in Slack. Integrated Tools: Instantly API connectors, Sendspark dynamic video endpoints, Mailbrevo delivery helpers, Looker tracking models.
Autonomy Bounds & Safety Stops: Band 1 generates drafts for human approval; Band 2 dispatches approved sequence templates autonomously after 50 clean reviews; Band 3 executes minor copy and timing
optimizations within send caps; Hard Stop — cold outbound SMS is hard-blocked; cannot modify commercial pricing terms or bypass daily mailbox limits.

4. Lead Agent / Reply Triage
Mission: Ingest, classify, and route 100% of inbound communications within 5 minutes, ensuring no qualified intent is delayed or mishandled.

````

<a id="source-b-page-32"></a>

## Source B — Page 32

````text
 LEAD AGENT INTENT TAXONOMY & ROUTING STATE MACHINE

                                     [Inbound Message Received via Webhook]
                                                         ▼
                                     ┌───────────────────────────────────────┐
                                     │ Multi-Class Intent Classifier         │
                                     └───────────────────┬───────────────────┘
          ┌──────────────────┬───────────────────────────┼───────────────────────────┬──────────────────┐
          ▼                  ▼                           ▼                           ▼                  ▼
 ┌──────────────────┐ ┌──────────────────┐      ┌──────────────────┐      ┌──────────────────┐ ┌──────────────────┐
 │ `HOT_LEAD` /     │ │ `QUESTION` /     │      │ `LATER`          │      │ `UNSUBSCRIBE`    │ │ `LEGAL_GRIEF`    │
 │ `WHALE_OWNER`    │ │ `OBJECTION`      │      │ (Timing Signal) │       │ (Opt-Out Signal) │ │ (Risk Signal)    │
 ├──────────────────┤ ├──────────────────┤      ├──────────────────┤      ├──────────────────┤ ├──────────────────┤
 │ Halts cold seq; │ │ Evaluates KB;     │      │ Ingests date to │       │ Deterministic    │ │ Tripping circuit │
 │ builds 1-screen │ │ drafts auto-reply│       │ Reactivation     │      │ opt-out write to │ │ breaker; halts   │
 │ context card;    │ │ if conf ≥90%,│      │ memory; pauses   │      │ DB suppression   │ │ sequence; alerts │
 │ sets SLA timers. │ │ else queues.     │      │ active campaign. │      │ (Zero human).    │ │ P0 to executive. │
 └────────┬─────────┘ └────────┬─────────┘      └────────┬─────────┘      └────────┬─────────┘ └────────┬─────────┘
          ▼                    ▼                         ▼                         ▼                    ▼
    #blackink-setter     #sales-replies            Reactivation Queue        Compliance Gate      #blackink-command



Core Responsibilities: Parses inbound messages across 10 operational intent classes; executes deterministic opt-out writes immediately upon detecting unsubscribe intent; generates 1-screen context cards for
setter/closer queues; enforces tiered response SLAs (15/60/240 minutes); operates 24/7 after-hours and weekend mode; ingests client-side service questions and complaints with the same SLA timing.
Input Interfaces: Inbound email webhooks, Twilio SMS webhooks, website concierge form submissions. Output Interfaces: Slack context cards, automated knowledge-base replies, calendar booking links,
compliance suppression writes. Integrated Tools: LLM intent classification pipelines, knowledge-base vector stores, Twilio SMS APIs, Calendly webhook listeners.
Autonomy Bounds & Safety Stops: Band 1 classifies intent and routes drafts to Slack queues; Band 2 auto-dispatches approved KB answers upon human confirmation; Band 3 sends high-confidence knowledge
base responses (≥90% confidence) for routine queries; Hard Stop — all legal threats, opt-outs, and negative complaints bypass automated generation and halt outbound actions immediately.

5. Reactivation & Nurture Agent
Mission: Maximize the lifetime value of existing data assets by systematically converting old inquiries, unclosed proposals, no-shows, and churn-risk properties into signed revenue.

 REACTIVATION & NURTURE TIMING MEMORY ENGINE

 [Inbound Timing Signal: "Call back in January"] ──► [Structured Date Extraction] ──► [Timing Memory Store]
                                                                                               ▼
                                                                                [Monitors Calendar Dates]
                                                                                               ▼
 [Outbound Re-Engagement Dispatched] ◄── [Context Re-Assembled] ◄── [Target Date Arrives (e.g., Jan 15)]
                ├── Recalls Previous Thread History & Stated Objections
                ├── References Property Address & Initial Audit Score
                └── Applies Fresh Value Hook (New Market Comps / Ryse Rent Advance)



Core Responsibilities: Maintains an active Timing Memory Store capturing future re-engagement dates; executes the Win-Back recipe against client historical dead leads; coordinates no-show recovery workflows;
operates the Churn Tripwire save workflow; manages client win-back workflows for churned Blackink clients themselves.
Input Interfaces: CRM dead-lead exports, historical meeting logs, timing metadata from Lead Agent triage, county deed/listing alerts. Output Interfaces: Win-Back outbound campaigns, no-show recovery emails,
retention save cards in Slack. Integrated Tools: PostgreSQL temporal scheduler, Instantly campaign connectors, Churn Tripwire public record monitors.
Autonomy Bounds & Safety Stops: Band 1 flags upcoming dates and drafts re-engagement messages; Band 2 fires win-back batches upon one-click approval; Band 3 automatically schedules future-dated re-
engagement touches within established cadences; Hard Stop — cannot contact any property owner currently listed in an active client's active management database.

6. Referral Agent
Mission: Establish a compounding local referral network by identifying, nurturing, and monetizing relationships with real estate agents, vendors, lenders, and industry partners.

 REFERRAL & PARTNER ECOSYSTEM ENGINE

 [Partner Discovery: Realtors / Lenders / Vendors] ──► [Specialized B2B Partner Outreach] ──► [Books Partner Meeting]
                                                                                                    ▼
                                                                                     [Human Manages Relationship]
                                                                                                    ▼
 [Referral Credit Ledger Updated] ◄── [Signed Agreement Detected] ◄── [Realtor Refers Investment Owner]
                ├── Tracks $250 Client-to-Client Referral Credits
                ├── Tracks $500 New-County Referral Credits
                └── Ingests Ryse & Partner Menu Monetization Events ($50/door in 4 tranches)



Core Responsibilities: Identifies and prioritizes local professional partners; executes specialized B2B partner sequences establishing referral relationships; manages Partner Menu enrollment workflows coordinating
onboarding integrations with vendors like Ryse; operates the Referral Credit Ledger; captures client wins as marketing proof (testimonial requests, case study drafts, badge nominations).
Input Interfaces: Local Realtor MLS rosters, vendor lists, client growth elections, verified signed=TRUE outcome events. Output Interfaces: Partner outbound sequences, referral attribution ledger entries,

````

<a id="source-b-page-33"></a>

## Source B — Page 33

````text
automated case study drafts. Integrated Tools: Partner CRM schemas, review aggregation APIs, Looker referral attribution models.
Autonomy Bounds & Safety Stops: Band 1 drafts partner outreach and logs referral sources; Band 2 enqueues testimonial and referral requests upon verified agreement milestones; Hard Stop — all vendor
agreements, legal fee-sharing contracts, and financial disbursements require human execution.

7. Economics & Growth Agent (Vera)
Mission: Continuously track unit economics, manage client quotas, enforce deterministic spending governors, calculate per-tenant contribution margins, and ensure accurate financial reporting.

 ECONOMICS GOVERNOR & PAID WALLET CONTROLLER

                                      [Events & Attribution Ledger Ingestion]
                                                         ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Per-Client Variable Cost Ledger (`COST-LEDGER`)                                                                      │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ Calculates Net Margin: Gross Revenue - (Enrichment + Twilio + Instantly Mailboxes + LLM Tokens + Paid Media Spend)   │
 └───────────────────────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                                         ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Deterministic Economics Governor Bands                                                                               │
 ├──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┤
 │ • Green Band (<$200 / Signed Deal)   ──► Fully funded; auto-allocates expansion budget                              │
 │ • Yellow Band ($200–$300 / Deal)     ──► Monitored; maintains current volume                                        │
 │ • Orange Band ($300–$400 / Deal)     ──► Trims high-cost enrichment and dials back ad spend                         │
 │ • Red Band (>$400 / Deal)            ──► Hard pause on paid channels; flags alert to Slack #blackink-economics      │
 └───────────────────────────────────────────────────────┬──────────────────────────────────────────────────────────────┘
                                                         ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Hard-Capped Paid Wallet Engine: Enforces strict spend limits ($150–$300 initial risk) in database                    │
 └──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────┘



Core Responsibilities: Operates the Opportunity Quota Engine, tracking target goals, booked meetings, held appointments, signed doors, and revenue forecasts per tenant; enforces the Deterministic Economics
Governor; manages the Paid Wallet Engine; calculates the Client Health Score; monitors collections and billing health; compiles monthly books-ready financial export packs.
Input Interfaces: Stripe transaction webhooks, Instantly mailbox costs, Twilio usage metrics, LLM token logs, appointment outcome rows. Output Interfaces: Daily Growth Reviews in Slack, Looker financial
dashboards, books-ready CSV exports, Stripe billing commands. Integrated Tools: Stripe Billing API, Looker SQL modeling engine, PostgreSQL financial transaction ledger.
Autonomy Bounds & Safety Stops: Band 1 calculates economics, monitors margins, and drafts financial reports; Band 2 triggers dunning recovery emails and queues budget reallocations; Hard Stop — cannot
modify commercial pricing tiers, issue cash refunds, or alter wallet spend ceilings without human authorization.

8. QA / Watchdog Agent
Mission: Ensure system integrity by actively monitoring infrastructure health, validating tenant isolation, enforcing suppression rules, and auto-recovering from transient faults.

 QA / WATCHDOG RESILIENCE & ADVERSARIAL TEST HARNESS

 ┌───────────────────────────────────────┐             ┌────────────────────────────────────────┐
 │ Nightly Suppression & Isolation Tests │             │ Infrastructure & Deliverability Health │
 ├───────────────────────────────────────┤             ├────────────────────────────────────────┤
 │ • Injects synthetic cross-tenant data │             │ • Monitors API latency & webhooks      │
 │ • Validates non-poach suppression     │             │ • Tracks domain reputation & bounce % │
 │ • Asserts row-level database security │             │ • Detects stale data & queue delays    │
 └──────────────────┬────────────────────┘             └───────────────────┬────────────────────┘
                    └──────────────────────────┬───────────────────────────┘
                                               ▼
 ┌──────────────────────────────────────────────────────────────────────────────────────────────┐
 │ Automated Self-Healing & Incident Escalation Subsystem                                       │
 ├──────────────────────────────────────────────────────────────────────────────────────────────┤
 │ 1. Transient API Failure ──► Retries with exponential backoff (Max 3 attempts)                │
 │ 2. Domain Reputation Degradation ──► Quarantines domain & rotates to warmed backup pool       │
 │ 3. Critical Failure / Tenancy Leak ──► Trips Emergency Circuit Breaker; alerts #blackink-qa    │
 └──────────────────────────────────────────────────────────────────────────────────────────────┘



Core Responsibilities: Executes nightly automated regression suites running adversarial cross-tenant data leakage tests; audits suppression integrity; monitors deliverability infrastructure across all 20 active
sending domains; evaluates system heartbeats; auto-adjudicates appointment disputes; implements self-healing routines.
Input Interfaces: UptimeRobot webhooks, database error logs, mail deliverability telemetry, appointment dispute tickets. Output Interfaces: Incident alerts in #blackink-qa , automated domain quarantine
triggers, dispute verdict logs. Integrated Tools: Pytest adversarial test suites, Sentry error monitoring, UptimeRobot, PostgreSQL audit loggers.
Autonomy Bounds & Safety Stops: Band 1 monitors health metrics and posts incident alerts; Band 2 recommends mailbox domain rotations and dead-letter queue re-runs; Band 3 automatically restarts safe
worker containers and quarantines degraded domains; Hard Stop — cannot override compliance gate errors or dismiss security assertion failures.

9. Setter Copilot

````

<a id="source-b-page-34"></a>

## Source B — Page 34

````text
Mission: Maximize the conversation-to-booking conversion rate of human setters and closers by generating real-time context briefs, recommended talk-tracks, and post-call analysis.

 SETTER COPILOT CALL CONTEXT & TRANSCRIPT ANALYSIS

 [Hot Lead / Booked Call Scheduled] ──► [Assembles 1-Screen Context Card] ──► [Posts to Slack #blackink-setter]
                                                                                             ▼
                                                                              [Human Conducts Phone Call]
                                                                                             ▼
 [Learned Conversion Patterns Stored] ◄── [Rubric Scoring & Objections] ◄── [Call Transcript Ingested]



Core Responsibilities: Assembles unified 1-Screen Context Cards for every scheduled discovery call and prospect dial; suggests tailored opening hooks, targeted discovery questions, and objection battle-cards;
manages the daily calling and follow-up queue inside #blackink-setter ; ingests call recordings and transcripts, scoring conversations against sales rubrics; integrates with the Referral Agent upon detecting positive
sales closes.
Input Interfaces: Calendly booking payloads, call recording webhooks, prospect engagement telemetry, CRM thread histories. Output Interfaces: Context cards in #blackink-setter , drafted post-call follow-ups,
transcript analysis logs. Integrated Tools: Transcription APIs, Block Kit interactive interfaces, calendar sync connectors.
Autonomy Bounds & Safety Stops: Band 1 generates call briefs, suggests talk-tracks, and drafts follow-up notes; Band 2 enqueues drafted post-call summaries and reminders for one-click dispatch; Hard Stop —
all verbal discovery conversations, qualification decisions, and contract negotiations remain strictly human.

````

<a id="source-b-page-35"></a>

## Source B — Page 35

````text
5.3 Cross-Agent Orchestration, Work-Orders & Locking Protocols
Every operational action across the nine agents is represented as a formal, typed Work-Order in the shared database:

 -- Schema: src/db/migrations/008_work_orders.sql

 CREATE TABLE agent_work_orders (
     action_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
     entity_id UUID NOT NULL,
     opportunity_id UUID,
     agent_id VARCHAR(50) NOT NULL, -- e.g. 'PROSPECTING_AGENT', 'CAMPAIGN_AGENT', 'LEAD_AGENT'
     action_class VARCHAR(100) NOT NULL, -- e.g. 'DISPATCH_EMAIL_TOUCH', 'ENQUEUE_DIAL_TASK'
     autonomy_band VARCHAR(20) NOT NULL, -- 'BAND_1_OBSERVE', 'BAND_2_ONE_TAP', 'BAND_3_AUTO'
     risk_class VARCHAR(20) NOT NULL,    -- 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'
     confidence_score NUMERIC(5,2) NOT NULL,
     payload_hash VARCHAR(64) NOT NULL,
     payload JSONB NOT NULL DEFAULT '{}'::jsonb,
     status VARCHAR(50) DEFAULT 'QUEUED',
     approved_by_user_id VARCHAR(100),
     approval_timestamp TIMESTAMP WITH TIME ZONE,
     execution_receipt JSONB,
     due_at TIMESTAMP WITH TIME ZONE,
     created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
     updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );


To prevent multiple agents from executing contradictory actions on the same entity (e.g., the Campaign Agent sending a cold outreach email while the Reactivation Agent initiates a win-back sequence), the Shared
Agent Core enforces distributed leases via Redis:

 # Location: src/core/orchestration/lease_manager.py
 import redis
 import hashlib
 import json

 class EntityLeaseManager:
     def __init__(self, redis_client: redis.Redis):
         self.redis = redis_client

      def acquire_entity_lease(self, client_id: str, entity_id: str, agent_id: str, ttl_seconds: int = 300) -> bool:
          """
          Acquires an exclusive operational lease on an entity.
          Prevents competing agents from acting on the same target concurrently.
          """
          lease_key = f"lease:{client_id}:{entity_id}"
          lease_payload = json.dumps({"agent_id": agent_id, "acquired_at": str(datetime.utcnow())})

         # Atomically set key if not exists (NX) with Time-To-Live expiration (EX)
         acquired = self.redis.set(lease_key, lease_payload, nx=True, ex=ttl_seconds)
         return bool(acquired)

      def release_entity_lease(self, client_id: str, entity_id: str, agent_id: str):
          lease_key = f"lease:{client_id}:{entity_id}"
          current_lease = self.redis.get(lease_key)
          if current_lease:
              data = json.loads(current_lease)
              if data.get("agent_id") == agent_id:
                  self.redis.delete(lease_key)


End-to-End Inter-Agent Execution Trace

Step           Agent Acting                                   Action Executed & System State Change

 1.            Prospecting Agent                              Ingests county records; resolves multi-LLC owners; calculates Owner Score; writes prospect record to companies and contacts .

 2.            QA / Watchdog Agent                            Runs compliance lint: verifies DNC status, confirms zero cross-client non-poach conflicts, and tags record as EMAIL_COLD_ELIGIBLE .

 3.            Campaign Agent                                 Deploys Ghost-Shopper bot to target website; records response latency; compiles dynamic Loss PDF; injects Sendspark video ID; queues Email 1.

````

<a id="source-b-page-36"></a>

## Source B — Page 36

````text
4.   Lead Agent               Prospect replies: "Interested, how does this work?"; classifies intent as HOT_LEAD (96% confidence); halts outbound sequence; alerts #blackink-setter .

5.   Setter Copilot           Compiles 1-screen context card (portfolio size, response audit, video watch data); closer runs discovery call; prospect books onboarding demonstration.

6.   Launch Agent             Agreement e-signs; fires tokenized Onboarding Portal link; tracks 15 states; provisions isolated database schema; arms client campaigns.

7.   Economics Agent (Vera)   Nightly PMS sync confirms newly signed property agreement ( door_signed ); triggers 50% initial Stripe ACH charge; compiles dynamic Evidence Packet PDF; schedules Day 60 clawback
                              monitor.

````

<a id="source-b-page-37"></a>

## Source B — Page 37

````text
5.4 Unified Memory Spines & Continuous Learning Loops
Blackink captures operational evidence from the very first send, establishing an empirical learning foundation that compounds over time.

Learning Loop Name                         Operational Data Stored & Continuous System Utility

 1. Operational Memory                     Active tasks, deadlines, lease states, and dependencies. Ensures seamless state recovery after container restarts without duplicate work.

 2. Conversation Memory                    Complete communication history, objections, commitments, and sentiment tags across all channels. Eliminates repetitive or uncontextualized follow-ups.

 3. Standing Client Rules                  Tenant-specific brand guidelines, forbidden claims, geographic boundaries, target fee structures, and pricing models. Enforced consistently across all agents.

 4. Counterfactual Memory                  Logs human revisions, overrides, and rejected agent recommendations alongside stated reasons. Teaches agents when not to act and calibrates confidence scoring.

 5. Win/Loss Autopsy                       Structured teardowns of won vs. lost opportunities (lead source, door count, messaging angle, speed to lead, stated objections).

 6. Experiment Registry                    Logs every copy, subject line, and timing variant tested with sample sizes, statistical significance, and results to prevent re-testing dead ideas.

 7. Source Quality Loop                    Measures match rate, cost per usable record, and signed doors per source dollar across data vendors, automatically downgrading underperforming feeds.

 8. Closing / Transcript Loop              Ingests call recordings, rubric scores, and objection handling data to train Setter Copilot on conversation patterns that successfully convert.


Cross-Tenant Privacy Protection: All learned playbooks and contextual optimizations are sanitized of personally identifiable information (PII) and tenant-specific business data. Optimization weights transfer across
clients as generalized statistical heuristics, ensuring zero cross-client information leakage.

````

<a id="source-b-page-38"></a>

## Source B — Page 38

````text
5.5 System Reliability, Circuit Breakers & Incident Handling
Failure Scenario                                      Automated Detection Trigger                                                Deterministic Recovery Protocol

Third-Party API Outage (RentCast / CoreLogic / TCR)   Provider returns 5xx status or times out for 3 consecutive requests.       Fails gracefully to cached fallback data; labels outputs as DEGRADED ; queues background retries with
                                                                                                                                 exponential backoff.

Stale Data or Missing Key                             Data older than 30-day refresh window or required entity key is null.      Returns explicit UNKNOWN or ABSTAIN state; prevents zero-filling; alerts #blackink-qa .

Persistent Task Failure                               Job fails execution across 3 retry attempts.                               Moves payload to Dead-Letter Queue (DLQ); creates priority incident ticket in Slack.

Domain Reputation Drop                                Bounce rate >3% or spam complaints >0.08% within rolling 48-hour window.   Instantly quarantines degraded domain; routes traffic to warmed backup domain pool.

Cross-Tenant Isolation Breach                         Adversarial test or query detects record with conflicting client_id .      Trips Emergency Circuit Breaker; freezes outbound dispatch; alerts P0 to #blackink-command .

Emergency Operator Halt                               Human operator triggers /halt command in Slack workspace.                  Persistently freezes all schedulers and message queues in Redis/DB until explicit resume.

````

<a id="source-b-page-39"></a>

## Source B — Page 39

````text
5.6 Full-System Acceptance Standard (Definition of Done)
The Blackink Governed Nine-Agent Workforce is officially accepted and certified production-ready upon passing the complete End-to-End Multi-Agent Acceptance Contract:

Verification Scenario                          Demonstrated Operational Behavior Required for Acceptance

 1. Cold Growth Lifecycle                      Prospecting Agent resolves owner → Compliance Gate verifies non-poach → Campaign Agent runs ghost-shopper audit → Lead Agent triages reply → Setter Copilot prepares call card → Launch
                                               Agent onboards tenant → Economics Agent verifies PMS agreement and executes settlement.

 2. Churn Save Workflow                        Synthetic deed filing injected → Reactivation Agent detects listing → Alert posted to Slack → Save opportunity logged as door_saved in ledger.

 3. Inbound Weekend Mode                       Concierge inquiry submitted 9:00 PM Saturday → Lead Agent classifies intent <60s → Books discovery call directly to calendar with zero human intervention.

 4. Auto-Dispute Resolution                    Synthetic dispute submitted → QA Watchdog Agent evaluates duration log and transcript keywords → Programmatically renders verdict in <30 seconds.

 5. Adversarial Exclusion                      Synthetic cross-tenant lead injected → Non-poach compliance gate hard-blocks work creation and execution → Incident logged to #blackink-qa .


 6. Persistent Halt Hold                       Emergency /halt triggered in Slack → Background workers persistently stop → System state verified unchanged across container reboot.

 7. Two-Tenant Repeatability                   Full lifecycle executed on Golden Client, followed by immediate clean execution on a second test tenant via written runbook with ZERO code modifications.

````

<a id="source-c"></a>

# Source C — Blackink_Business_Need_End_to_End_Flow.docx

Complete source transcription. Apply the authority rules above before treating any instruction or code as current.

````text
[DOCX part: word/document.xml]
BLACKINK
Business Need & End-to-End Operating Flow
A non-technical explanation of what the business does once the platform is live

[TABLE]
Core business outcome
More managed doors + fewer lost doors + more revenue per existing door for residential property-management firms.
[/TABLE]
Prepared from the Project Blackink Complete Implementation Blueprint


Contents
1. What Blackink is
2. The three parties involved
3. Complete business lifecycle
4. Stage-by-stage end-to-end example
5. How Blackink gets paid
6. Retention and expansion after acquisition
7. Full numeric example
8. What Blackink is really selling

Reading note: This document intentionally explains the business model and operational flow. It does not discuss implementation code, servers, repositories, or engineering architecture.
1. What Blackink Is
Blackink is a growth and retention service for residential property-management companies.
A property-management company uses Blackink because it wants to get more property owners to sign management agreements, respond faster to owner inquiries, revive old or dead leads, stop existing owners from leaving, and increase revenue from its current portfolio.
The business is designed around measurable outcomes rather than simply selling software, AI, email automation, or raw lead lists.
[TABLE]
Business Goal | Meaning
Grow the book | Acquire new property owners and add new managed properties (doors).
Protect the book | Detect churn risk early and help keep existing owners/properties under management.
Increase value per door | Find revenue opportunities inside the existing portfolio, such as rent gaps and ancillary services.
[/TABLE]

2. The Three Parties Involved
[TABLE]
Party | Who they are | Example
Blackink | The growth platform/service operator. | Blackink
Blackink Client | A residential property-management company paying Blackink for growth and retention outcomes. | Tampa Premier Property Management
End Prospect | A landlord/property owner who may need a property manager. | Sarah owns four rental homes in Tampa
[/TABLE]

The most important distinction is that Blackink's customer is the property-management company. The landlord or property owner is the prospect Blackink helps the client acquire, convert, or retain.
3. Complete Business Lifecycle
[TABLE]
Blackink needs customers
        ↓
Find property-management companies
        ↓
Show them where they are losing business
        ↓
Book sales/demo
        ↓
Property-management company becomes a Blackink client
        ↓
Onboard client: territory, ideal owners, current book, old leads, pricing, calendar
        ↓
Launch growth engines
        ↓
Find / reactivate property owners
        ↓
Respond to owner inquiries extremely fast
        ↓
Generate qualified meetings
        ↓
Property-management company closes the owner
        ↓
New managed doors are added
        ↓
Blackink verifies the result and bills
        ↓
Blackink also monitors existing doors for churn and revenue opportunities
        ↓
Repeat continuously
[/TABLE]

4. Stage-by-Stage End-to-End Example
The following example uses a fictional client, Tampa Premier Property Management, to show how Blackink would operate once live.
Stage 1 — Blackink acquires a property-management company
Tampa Premier manages about 350 rental properties around Tampa.
It receives landlord inquiries through its website, but response times are slow.
It has hundreds or thousands of old leads sitting unused in its CRM.
It loses some owners every year and has no systematic churn-monitoring process.
[TABLE]
Business meaning: Blackink identifies Tampa Premier as a potential client and profiles the company, its market, approximate door count, owner/broker contact, and operations contact.
[/TABLE]
Stage 2 — Blackink proves there is a problem before selling
Blackink performs a ghost-shopper audit by submitting a realistic owner inquiry through Tampa Premier's public website.
Example inquiry: “I own three rental properties in Tampa and I'm considering switching property-management companies. Can someone contact me?”
Blackink measures exactly how long the company takes to respond.
[TABLE]
Business meaning: If the inquiry is sent at 9:00 AM and the response arrives at 1:12 PM, Blackink now has a concrete 4-hour-12-minute response gap. It can turn that into a Speed & Revenue Loss Report showing how slow response may cost management business.
[/TABLE]
Stage 3 — Blackink runs its own sales sequence
Day 0: personalized email with the speed audit and video.
Day 1–2: human phone call.
Day 4: follow-up email with additional revenue-loss or fee-stack evidence.
Day 7: LinkedIn/manual follow-up.
Day 10: final market or competitor angle.
Cold SMS is not the acquisition channel; SMS is reserved for permitted engaged/consented situations.
[TABLE]
Business meaning: The point is to avoid generic “we are a marketing agency” outreach. Blackink approaches the PM firm with proof of a specific operational problem.
[/TABLE]
Stage 4 — Blackink books and runs the sales/demo
The PM company replies and books a call.
Blackink can demonstrate tools such as a live rent-analysis flow using a real property address.
The sales story is focused on real-estate-specific growth, response speed, and measurable management revenue.
[TABLE]
Business meaning: The goal is to get the property-management company to sign Blackink as a growth and retention partner.
[/TABLE]
Stage 5 — The PM company becomes a Blackink client
The client signs an agreement.
Blackink captures billing authorization.
Blackink collects business contacts, PM profile, territory, calendar, offer/CTA, historical data, documents, current owner book, and growth goals.
The client progresses through a structured onboarding and preflight process before campaigns are armed.
[TABLE]
Business meaning: At this point Blackink stops marketing itself to this firm and begins operating on behalf of the client.
[/TABLE]
Stage 6 — Blackink learns the client's Ideal Customer Profile (ICP)
Example client preference: single-family homes and 2–10 unit multifamily.
Target areas: Tampa, Brandon, Riverview.
Minimum acceptable rent: $1,500/month.
No short-term rentals.
Preference for owners with 3+ doors.
[TABLE]
Business meaning: This tells Blackink which landlords should be found, contacted, qualified, and ultimately sent to this specific client.
[/TABLE]
Stage 7 — Blackink imports the client's existing business
Active Book: owners and properties the client already manages.
Dead Book / Historical Leads: old inquiries, cancelled agreements, no-shows, and prospects that never signed.
[TABLE]
Business meaning: The active book is used for suppression/non-poach and retention monitoring. The dead book becomes a reactivation opportunity instead of wasted historical data.
[/TABLE]
Stage 8 — The client goes live with multiple growth engines
Speed-to-Lead for fresh inbound owner inquiries.
Win-Back for old/dead leads.
New owner prospecting using public/property intelligence.
Referral and partner channels.
Retention monitoring for existing managed doors.
[TABLE]
Business meaning: Blackink is not dependent on one lead source. Multiple engines feed the same client growth system.
[/TABLE]
Stage 9 — A new inbound owner inquiry is handled fast
David submits: “I have two rentals in Tampa and I'm considering hiring a property manager.”
Blackink detects the inquiry immediately.
The inquiry is validated and checked against business/compliance rules.
A confirmation and booking opportunity is sent.
The client's closer/salesperson is alerted.
[TABLE]
Business meaning: The business objective is to contact a hot landlord lead in under a minute rather than allowing it to sit for hours while competitors respond first.
[/TABLE]
Stage 10 — Old leads are reactivated
Jennifer contacted the PM firm 14 months ago about three rentals.
She said: “Not ready right now. Maybe next year.”
Blackink remembers the timing signal and re-engages near the relevant date with the previous context intact.
[TABLE]
Business meaning: The client can generate revenue from historical records it already paid to acquire instead of continually buying fresh leads.
[/TABLE]
Stage 11 — Blackink finds entirely new property owners
Public property and business records are used to identify promising owners.
LLCs can be resolved to the same beneficial owner so separate property records become one meaningful portfolio.
The system prioritizes owners with meaningful door counts and fit for the client's territory/ICP.
[TABLE]
Business meaning: Example: three different LLCs appear to own 3, 2, and 3 homes. Blackink resolves them to the same individual and recognizes an eight-door owner — a much better prospect than three disconnected LLC records.
[/TABLE]
Stage 12 — Prospect replies and is triaged
Michael, an eight-door owner, replies: “Possibly interested. What do you charge?”
Blackink recognizes this as high intent.
The PM firm's setter receives the owner identity, estimated door count, previous touches, audit context, and suggested talking points.
[TABLE]
Business meaning: Blackink prepares and routes the opportunity. The human salesperson remains responsible for the actual sales conversation, qualification decisions, negotiation, and closing.
[/TABLE]
Stage 13 — A qualified appointment is generated
Michael books a meeting with Tampa Premier.
The PM company's human representative holds the sales conversation.
Blackink records attendance and the qualification evidence.
[TABLE]
Business meaning: For an appointment to be considered a verified billable outcome under the blueprint model, it should satisfy the agreed verification conditions rather than merely existing on a calendar.
[/TABLE]
Stage 14 — Verify the business outcome
Rule 1: real residential property/ownership is verified in the contracted territory.
Rule 2: the attended meeting lasts at least 12 minutes.
Rule 3: there is documented pre-qualification/intent.
Rule 4: the prospect matches the client's agreed ICP.
[TABLE]
Business meaning: If Michael owns eight real Tampa rentals, attends a 32-minute meeting, expressed management interest, and matches the ICP, the result becomes a qualified attended outcome.
[/TABLE]
Stage 15 — Owner signs a management agreement
Michael signs management agreements for all eight rentals.
Tampa Premier goes from 350 managed doors to 358 managed doors.
Those doors generate ongoing management revenue for the client.
[TABLE]
Business meaning: This is why Blackink focuses on “doors,” not just lead counts. A single multi-property owner can represent significant recurring revenue.
[/TABLE]
Stage 16 — Blackink verifies and gets paid
The blueprint contains event-based appointment pricing, packs, subscriptions, performance settlement, and other commercial offers.
The core principle is that Blackink wants payment tied to measurable outcomes, not merely the number of emails sent or records generated.
[TABLE]
Business meaning: Depending on the offer, billing can be tied to verified attended appointments, signed outcomes, packages, recurring subscriptions, or other defined commercial products.
[/TABLE]
Stage 17 — Blackink protects the client's existing portfolio
The system watches active managed properties for risk signals such as MLS/FSBO listings, deed transfers, homestead changes, mailing-address changes, and agreement anniversaries.
When risk is detected, the client is alerted early enough to act.
[TABLE]
Business meaning: Example: Sarah's property appears for sale. Blackink alerts Tampa Premier, the PM firm contacts Sarah proactively, and if the management relationship is preserved the outcome can be recorded as a saved door.
[/TABLE]
Stage 18 — Blackink increases revenue inside the portfolio
Rent Gap Report identifies units renting materially below market.
Owner Report Card gives landlords a professional, branded performance report.
Ancillary/partner services create additional revenue opportunities.
[TABLE]
Business meaning: The business moves beyond acquisition: Blackink tries to help the PM company earn more from the portfolio it already manages while improving owner retention.
[/TABLE]
Stage 19 — Referrals and partner growth compound
Blackink can cultivate Realtors, lenders, vendors, and local partners.
Example: a Realtor refers an investor who just acquired six rentals.
The investor can become a new PM client opportunity for the property-management firm.
[TABLE]
Business meaning: Over time this reduces dependence on cold outbound and creates additional acquisition channels.
[/TABLE]
5. How Blackink Gets Paid
The blueprint does not define only one commercial model. It includes several offer types, but the consistent business idea is outcome-linked monetization.
[TABLE]
Example Outcome / Offer | Blueprint Example
Qualified owner appointment: 1–4 doors | $175 per event
Qualified owner appointment: 5–9 doors | $350 per event
Qualified owner appointment: 10+ doors | $500 per event
Large/principal opportunity | $750 per event
Inbound response service | $249/month
Retention Guard | $497/month
Rent Gap Report | $197/month
Owner Report Card | $197/month
Growth OS | $897 / $997 per month
[/TABLE]

The blueprint also describes a zero-deposit / performance-settlement concept in which the client authorizes payment upfront but charges occur only after verified outcomes, with a 50/50 settlement concept and a later verification/clawback step for applicable offers.
The simplest business interpretation is: Blackink should be paid for verified commercial value — qualified appointments, signed management outcomes, retained doors, or subscribed growth/retention services — not for activity metrics alone.
6. Retention and Expansion After Acquisition
[TABLE]
Problem | Blackink Response | Business Result
Fresh owner inquiries respond slowly | Speed-to-Lead | Higher chance of booking the owner before a competitor does.
Historical leads are dormant | Win-Back / Dead-Book | Recover value from old CRM data.
Need new landlords | Prospecting / owner intelligence | Create new qualified owner opportunities.
Existing owner may leave | Churn Tripwire / Retention Guard | Protect managed doors and recurring management revenue.
Rents are below market | Rent Gap Report | Support rent increases and potentially increase management-fee revenue.
Owners do not clearly see value | Owner Report Card | Improve owner communication and retention.
Need additional acquisition channels | Referral / partner network | Generate owner opportunities through local partners.
[/TABLE]

7. Full Numeric Example
Before Blackink
[TABLE]
350 active managed doors
500 inbound owner inquiries per year
Response time often measured in hours
1,800 historical dead leads
20–30 owners leave annually
No systematic owner-prospecting engine
No structured reactivation system
No churn-monitoring engine
[/TABLE]

After onboarding
[TABLE]
Import 350 active managed doors
Import 1,800 historical leads
Define Tampa territory
Define ideal owner / property profile
Connect calendar
Activate inbound response
Activate win-back
Activate new-owner acquisition
Activate retention monitoring
[/TABLE]

Illustrative month
[TABLE]
Metric | Illustrative Result
New owner prospects identified | 200
Old leads reactivated | 80
Inbound owner inquiries | 25
Meaningful conversations | 35
Meetings booked | 18
Qualified attended meetings | 12
Owners signed | 4
New managed doors | 16
Retention alerts | 6
Owners retained after intervention | 2
Existing doors saved | 7
[/TABLE]

The business story Blackink should tell the client is not: “We sent 8,000 emails.”
[TABLE]
“This month we generated 12 verified owner appointments, contributed to 16 new managed doors, and helped protect another 7 existing doors.”
[/TABLE]
8. What Blackink Is Really Selling
Blackink is not primarily selling AI, software, cold-email automation, or data. Those are mechanisms.
The actual product is business growth and retention for property-management firms:
[TABLE]
Business Problem | Blackink Solution
PM firm needs more owners | Owner acquisition
Leads are not answered quickly | Speed-to-Lead
Old leads are wasted | Win-Back / Dead-Book
Sales reps lack context | Setter support
Good prospects are not followed up | Reactivation
Existing owners may leave | Churn Tripwire / Retention Guard
Rents are below market | Rent Gap Report
Owners do not see enough value | Owner Report Card
PM company needs referral channels | Partner / Referral network
Blackink needs proof it created value | Outcome verification
Client disputes a charge | Evidence and dispute verification
Client wants new markets / products | Expansion offers and county growth
[/TABLE]

The intended end state is a repeatable “business-in-a-box”: Blackink should be able to take a new property-management company, onboard it, launch growth services, service the account, measure verified outcomes, bill for the value created, and repeat the process for the next client without reinventing the business each time.
[TABLE]
Clean mental model
Blackink finds opportunities → gets owners into conversations → helps the PM company close them → verifies the result → protects the resulting portfolio → gets paid from the value created.
[/TABLE]

Source basis: Project Blackink — Complete Implementation Blueprint (Full), including the acquisition, onboarding, growth, verification, retention, expansion, and business-in-a-box lifecycle described in the blueprint.

[DOCX part: word/header1.xml]
PROJECT BLACKINK  |  BUSINESS OVERVIEW

[DOCX part: word/footer1.xml]
Blackink Business Need & End-to-End Operating Flow  •  
````

<a id="source-d"></a>

# Source D — Blackink - DoorEngine Unified System Specification.pdf

Complete source transcription. Apply the authority rules above before treating any instruction or code as current.

<a id="source-d-page-1"></a>

## Source D — Page 1

````text
UNIFIED SYSTEM & ENGINEERING SPECIFICATION

Blackink / DoorEngine — System Specification
Client                   Josh Kantor, Blackink

Lead Developer           Hari Krishnan (heu.ai)




1. System Architecture & Scope Boundaries

The Blackink / DoorEngine platform operates as an integrated property-manager growth engine structured around four revenue-recovery pipelines
and four corresponding leakage pools.

 Revenue Engine            Target Leakage Pool            Operational Mechanism & Technical Output

 1. Respond Engine         Inbound Delay Pool             Sub-60-second automated multi-channel response to incoming owner inquiries across webhooks and
                                                          portal parsers.

 2. Revive Engine          Dead-Lead Pool                 Systematic reactivation and re-qualification of dormant CRM owner inquiries and past proposals.

 3. Retain Engine          Owner Churn Pool               Inward-facing monitoring of client books for sell, listing, deed, and homestead signals before churn
                                                          occurs.

 4. Grow Engine            Unworked Acquisition Pool      Nightly public-record scanning, distress-stack scoring, and outbound acquisition of in-market rental
                                                          owners.


Architectural Scope Boundaries
  Vendor Screens Retained: The vendor-facing interface is included in current delivery milestones. It provides signal-surfaced dashboards and
  partner monetization tracking without custom AI response pipelines built on their behalf.
  Realtor Network Deferred to Future Release: The realtor-facing user interface, realtor rank pages, referral-clock logic, and manager-to-
  manager referral credits are excluded from current delivery and deferred to a later phase.
  Florida §475 Legal Schema Constraint: Under Florida Statute §475, direct brokering of introductions between licensed real estate brokers and
  third parties carries brokerage licensing exposure. The database schema and telemetry logging must record signal_surfaced_to events rather
  than introduction_made events. The platform operates strictly as software routing public data signals to subscribers.
  Property Management Software (PMS) OAuth Ingestion Deferred: Direct OAuth2 API connectors for AppFolio, Buildium, and Rentvine are
  excluded from current delivery and scheduled for a future milestone. Tenant data ingestion operates via secure manual CSV drops, webhook
  endpoints, and forwarded email parsing.

````

<a id="source-d-page-2"></a>

## Source D — Page 2

````text
2. The Client-Facing Presentation Layer (The Six Demo Screens)

The property-manager-facing presentation layer consists of six dedicated views designed to establish credibility with skeptical prospects who have
never interacted with the platform. All copy, pricing parameters, door-count thresholds, and fee percentages must be driven by external
configuration files rather than hardcoded in the codebase.

 PRESENTATION LAYER TOPOLOGY (THE SIX SCREENS)

 [Screen 1: The Owner Packet]      ──► Live-filtered public record distress records with pull timestamps
 [Screen 2: The Money Map]         ──► Dynamic compounding ROI calculator reading real-time inputs
 [Screen 3: The Guarantee Panel]   ──► Equal-weight guarantee block and five-line fairness policy
 [Screen 4: The Empty Scoreboard] ──► Zero-data table layout with real column headers
 [Screen 5: Speed-to-Lead Trigger] ──► Sub-60-second live test fire demonstrating response automation
 [Screen 6: The Deliberate Kill]   ──► Vendor/realtor network view stamped "PLANNED - NOT LIVE"



Screen 1: The Owner Packet
The Owner Packet serves as the initial demonstration asset. It displays 5 to 10 named property owners in the prospect's contracted postal codes who
have exhibited distress signals within the prior 14 days.
Display Schema — every row must render: legal owner name and physical property address; aggregated portfolio door count owned across the
county; estimated market rental value per month; property postal code; visible distress tags ( EVICTION_FILED , TAX_DELINQUENT , PROBATE_TRANSFER ,
 CODE_VIOLATION , PERMIT_PULLED , OUT_OF_STATE_OWNER ); algorithmic composite rank score; inferred management status ( SELF_MANAGED or
 THIRD_PARTY_MANAGED ); rent advance eligibility indicator ( OCCUPIED , LEASE_LENGTH_OK , NOT_CURRENTLY_LISTED ); and a mandatory timestamp of exact
date, time, and source county of extraction (e.g., Pulled: 2026-09-01 04:12:08 UTC — Hillsborough Clerk ).
Filter Controls: Dynamic UI inputs for target postal codes, door-count thresholds, asset classifications, and date boundaries. Export Pipeline:
Headless export generating a clean, single-page executive PDF that can be delivered in advance or handed over during a meeting.

Screen 2: The Money Map
An interactive, client-side compounding revenue calculator populated live during the sales demonstration using the prospect's operational metrics.
   Dynamic Input Parameters: Property management fee percentage (configured with standard market bounds of 8% to 12%, defaulting to 10%);
   average monthly market rent per unit; average doors acquired per owner relationship; historical close rate percentage on held appointments.
   Calculated Output Metrics: Net monthly management fee revenue generated per converted owner; gross leasing and renewal fee capture
   realized upon initial tenant turnover; compounding revenue and door-accumulation schedule modeled at Month 1, Month 4, and Month 12; explicit
   breakdown separating first-year cash collected from cumulative ARR added to the rent roll.
   Pending Rent Advance Row: A designated line item for rent advance participation revenue rendered with an explicit PENDING status. No
   monetary projections may populate this field until official terms are confirmed in writing.
   Arithmetic Invariant: No speculative enterprise valuation multiples may appear on this screen; calculations must remain strictly limited to
   verified fee income.

Screen 3: The Guarantee Panel & Five-Line Fairness Policy
A dedicated operational panel displaying the commercial guarantee and the 5-line fairness rules. The guarantee text must render at the identical
typographical scale and visual weight as the pricing figures.

 Rule Identifier                       Enforced Policy & System Invariant

 1. No-Show Exemption                  Zero billing occurs on an appointment where the owner fails to attend, regardless of the reason.

 2. Active CRM Deconfliction           Zero billing if the owner was an active prospect in the client's CRM within the prior 90 days.

 3. Criteria Mismatch Shield           Zero billing if the lead falls outside the client's initialed postal codes, door-count bands, or residential asset classes.

 4. Asset Filter Protection            Zero billing if the property is commercial-only, HOA-only, or listed for sale on the MLS at the time of appointment creation.

 5. Rapid Credit SLA                   Any dispute flagged by the client within 48 hours that cannot be verified is credited immediately without debate.


Exclusivity Constraint: The identical live prospect opportunity is never delivered or sold to two competing managers. Dual Confirmation
Precondition: The platform must execute automated appointment confirmations 24 hours and 3 hours prior to start. If confirmation dispatches fail
and the owner fails to appear, the appointment is unbillable.

Screen 4: The Empty Scoreboard
The Scoreboard view renders the operational tracking framework in an intentional zero-data state. It contains verified database column headers but
zero synthetic or mock records, reinforcing transparency with new prospects.
Visible Column Schema: Owners Contacted , Owners Reached , Appointments Set , Appointments Attended , Doors Signed , Net Management Revenue Added .
Header Controls: Filterable by client postal codes and dynamic date-range pickers.

Screen 5: The Speed-to-Lead Trigger
An interactive demonstration utility designed to test the platform's response automation during a live presentation. Execution Flow: An input field
captures a test phone number and fires a synthetic inbound owner inquiry via webhook. Operational SLA: The platform processes the payload,
executes compliance checks, and triggers an automated response (voice callback or SMS response) in under 60 seconds.

Screen 6: The Deliberate Kill Board
A mock interface of the vendor, contractor, and real estate agent referral network stamped with a prominent graphical overlay: PLANNED - NOT LIVE .
This demonstrates architectural discipline by distinguishing live systems from roadmap concepts.

````

<a id="source-d-page-3"></a>

## Source D — Page 3

````text
3. Supporting Sales & Operational Deliverables

The Pre-Appointment Owner Brief Dossier
The Owner Brief is a mandatory, single-page operational dossier compiled for every booked meeting. It bridges the gap between delivering a raw
calendar booking and providing an actionable business development meeting.
Mandatory Generation Gate: Delivery of an appointment payload to a client calendar requires an attached Owner Brief.
Payload Structure: Owner legal name, verified corporate entities, and known mailing addresses; detailed portfolio summary (parcel identifiers,
property addresses, estimated market rents); identified distress vectors and chronological timeline of public-record filings; assessed management
posture; algorithmic distress score and conversion rationale ("Why Now"); rent advance eligibility pre-check results; recommended opening script and
anticipated owner objections.

Physical Offer Sheet & Territory Zone Maps
Photographable Offer Sheet: A single-page, dual-sided layout formatted specifically for mobile capture. The front displays the core pricing and
guarantee block; the reverse displays the five-line fairness policy and contractual definitions.
Hillsborough Territory Map: A cartographic visualization partitioning Hillsborough County into defined postal code clusters. The Tampa
metropolitan area is divided into 3 to 4 distinct operational zones rather than a single territory, establishing clear boundaries for exclusivity.

````

<a id="source-d-page-4"></a>

## Source D — Page 4

````text
4. Appointment Operations State Machine, Verification & Dispute Engine

The appointment operations pipeline enforces the billing lifecycle. Under performance pricing, revenue realization requires verified operational proof.

 APPOINTMENT OPERATIONS STATE MACHINE

                                               [BOOKED]
                                                  │
                                                  ▼
                                        [CONFIRMED_24H]
                                                  │
                                                  ▼
                                        [CONFIRMED_3H]
                                                  │
                                                  ▼
                                            [ATTENDED]
                                                  │
                                                  ▼
                                           [DISPOSITIONED]

     Alternative Paths & Terminal Exceptions:
     • Any State ──► [RESCHEDULED] (Capped at 2 total reschedules before moving to [LOST])
     • Failed Confirmation / Attend ──► [NO_SHOW_RECOVERY] ──► [REBOOKED] (Same opportunity ID)
     • Terminal Cancellation ──► [LOST]



The Programmatic Billing Gate
An appointment transitions to billable status if and only if all predicates in the billing gate evaluate to TRUE :

                      Billable ⇔ (state = ATTENDED) ∧ (confirmed_24h_timestamp ≠ NULL) ∧ (confirmed_3h_timestamp ≠ NULL)

If an appointment takes place without verified 24-hour and 3-hour confirmation logs, it is unbillable. An opportunity that is rescheduled or recovered
after a no-show retains its primary opportunity_id to prevent duplicate billing. Reschedule attempts are capped at two; a third reschedule
automatically marks the opportunity as LOST .

Relational Schema Specification

````

<a id="source-d-page-5"></a>

## Source D — Page 5

````text
 -- Schema: src/db/migrations/009_appointment_ops.sql

 -- 1. APPOINTMENT STATE ENUM
 CREATE TYPE appointment_state_enum AS ENUM (
     'BOOKED', 'CONFIRMED_24H', 'CONFIRMED_3H', 'ATTENDED', 'DISPOSITIONED',
     'RESCHEDULED', 'NO_SHOW_RECOVERY', 'REBOOKED', 'LOST'
 );

 -- 2. APPOINTMENT TRACKING MASTER TABLE
 CREATE TABLE appointments (
     appointment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     client_id UUID NOT NULL REFERENCES companies(company_id) ON DELETE CASCADE,
     opportunity_id UUID NOT NULL, -- Preserved across reschedules to prevent double billing
     contact_id UUID NOT NULL REFERENCES contacts(contact_id),
     state appointment_state_enum NOT NULL DEFAULT 'BOOKED',
     reschedule_count INTEGER NOT NULL DEFAULT 0,
     scheduled_for TIMESTAMP WITH TIME ZONE NOT NULL,
     attended_at TIMESTAMP WITH TIME ZONE,
     is_billable BOOLEAN GENERATED ALWAYS AS (
         state = 'ATTENDED' AND
         confirmed_24h_timestamp IS NOT NULL AND
         confirmed_3h_timestamp IS NOT NULL
     ) STORED,
     confirmed_24h_timestamp TIMESTAMP WITH TIME ZONE,
     confirmed_3h_timestamp TIMESTAMP WITH TIME ZONE,
     owner_brief_url TEXT NOT NULL,
     created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
     updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );

 -- 3. CONFIRMATION AUDIT LOG
 CREATE TABLE confirmation_logs (
     log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     appointment_id UUID NOT NULL REFERENCES appointments(appointment_id) ON DELETE CASCADE,
     channel VARCHAR(10) NOT NULL CHECK (channel IN ('SMS', 'EMAIL')),
     confirmation_tier VARCHAR(10) NOT NULL CHECK (confirmation_tier IN ('24H', '3H')),
     sent_at TIMESTAMP WITH TIME ZONE NOT NULL,
     delivery_status VARCHAR(50) NOT NULL,
     reply_received_at TIMESTAMP WITH TIME ZONE,
     raw_response TEXT,
     created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );

 -- 4. DISPOSITION CAPTURE TABLE
 CREATE TABLE appointment_dispositions (
     disposition_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     appointment_id UUID UNIQUE NOT NULL REFERENCES appointments(appointment_id) ON DELETE CASCADE,
     outcome VARCHAR(20) NOT NULL CHECK (outcome IN ('SIGNED', 'DECIDING', 'NO', 'NOT_A_FIT')),
     doors_signed INTEGER NOT NULL DEFAULT 0,
     close_reason VARCHAR(50) CHECK (close_reason IN (
         'PRICE', 'TIMING', 'STAYING_SELF_MANAGED', 'WENT_ELSEWHERE', 'NOT_QUALIFIED'
     )),
     brief_accurate VARCHAR(10) NOT NULL CHECK (brief_accurate IN ('YES', 'PARTLY', 'NO')),
     disposition_captured_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );

 -- 5. DISPUTE AUDIT LOG
 CREATE TABLE appointment_disputes (
     dispute_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
     appointment_id UUID UNIQUE NOT NULL REFERENCES appointments(appointment_id) ON DELETE CASCADE,
     flagged_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
     reason TEXT NOT NULL,
     evidence_ref TEXT,
     outcome VARCHAR(50) NOT NULL DEFAULT 'CREDITED_AUTOMATIC',
     resolved_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
 );

 CREATE INDEX idx_appointments_billing_gate ON appointments(state, confirmed_24h_timestamp, confirmed_3h_timestamp);
 CREATE INDEX idx_opportunity_dedupe ON appointments(opportunity_id);



Dispute Resolution Engine
Clients possess a 48-hour window from the scheduled meeting time to flag an appointment. If an appointment outcome cannot be verified by the
platform, the credit issues automatically without human review. The dispute engine updates the accounting ledger and adjusts current billing lines.

Weekly Operational Rollup Query
Client performance is evaluated through an automated weekly SQL query that runs across appointment, financial, and disposition logs to calculate
efficiency metrics:

````

<a id="source-d-page-6"></a>

## Source D — Page 6

````text
-- Weekly Operational Rollup Query
-- Computes operational conversion and cost-per-door metrics per client
SELECT
    c.company_name AS client_name,
    COUNT(a.appointment_id) FILTER (WHERE a.created_at >= NOW() - INTERVAL '7 days') AS booked_count,
    COUNT(a.appointment_id) FILTER (WHERE a.state = 'ATTENDED' AND a.attended_at >= NOW() - INTERVAL '7 days') AS attended_count,
    COUNT(d.disposition_id) FILTER (WHERE d.outcome = 'SIGNED' AND d.disposition_captured_at >= NOW() - INTERVAL '7 days') AS signed_count,
    COALESCE(SUM(d.doors_signed) FILTER (WHERE d.disposition_captured_at >= NOW() - INTERVAL '7 days'), 0) AS doors_acquired,
    COALESCE(SUM(cost.expense_cents) FILTER (WHERE cost.incurred_at >= NOW() - INTERVAL '7 days'), 0) / 100.0 AS cost_this_week,
    CASE
        WHEN COUNT(a.appointment_id) FILTER (WHERE a.state = 'ATTENDED' AND a.attended_at >= NOW() - INTERVAL '7 days') > 0
        THEN (COALESCE(SUM(cost.expense_cents) FILTER (WHERE cost.incurred_at >= NOW() - INTERVAL '7 days'), 0) / 100.0) /
             COUNT(a.appointment_id) FILTER (WHERE a.state = 'ATTENDED' AND a.attended_at >= NOW() - INTERVAL '7 days')
        ELSE 0.00
    END AS cost_per_attended_sit,
    CASE
        WHEN COALESCE(SUM(d.doors_signed) FILTER (WHERE d.disposition_captured_at >= NOW() - INTERVAL '7 days'), 0) > 0
        THEN (COALESCE(SUM(cost.expense_cents) FILTER (WHERE cost.incurred_at >= NOW() - INTERVAL '7 days'), 0) / 100.0) /
             SUM(d.doors_signed) FILTER (WHERE d.disposition_captured_at >= NOW() - INTERVAL '7 days')
        ELSE 0.00
    END AS cost_per_door
FROM companies c
LEFT JOIN appointments a ON a.client_id = c.company_id
LEFT JOIN appointment_dispositions d ON d.appointment_id = a.appointment_id
LEFT JOIN client_cost_ledger cost ON cost.client_id = c.company_id
WHERE c.status = 'CLIENT'
GROUP BY c.company_id, c.company_name;

````

<a id="source-d-page-7"></a>

## Source D — Page 7

````text
5. Catalog Architecture & Entitlement Database Integration

The Catalog Collision & Resolution
A pricing collision emerged between legacy platform specs and the founding territory offer:
   In earlier documentation, $397/month represented the additional county expansion add-on, restricted to established accounts.
   In the founding territory offer, $397/month represents the base monthly territory access rate for Hillsborough County.
   The per-sit fee was reconciled at $99 per attended sit to align with existing metered catalog rows, rather than introducing a separate $100 line
   item.

The Architectural Fix: Add a single dedicated commercial row ( owner_growth_founding ) to the entitlement_offers registry without restructuring
existing tier hierarchies.

 Field                                               Value

 Offer ID                                            owner_growth_founding

 Display Name                                        Owner Growth — Hillsborough Founding Seat

 Monthly Base Price                                  $397.00 / month (Stripe Subscription ID: price_founding_hillsborough_base )

 Metered Show Price                                  $99.00 / verified attended sit (Stripe Metered ID: price_founding_sit_meter )

 Target Audience                                     Property Managers (Three founding seats, Hillsborough County only)

 Rate Protection                                     Founding rate locked for 6 months; converts to standard Owner Growth ($749/mo + $99/sit)

 First-Show Logic                                    First sourced attended sit is provided at $0; enforced via entitlement ledger

 Status                                              ENABLED


Complete Commercial Tier Ladder
All commercial terms exist strictly as database rows in the offer registry:

 Plan Tier                         Monthly Base                    Per Attended Sit                Territory Scope & Inclusion

 Founding Zone                     $397 / month                    $99 / sit                       3 to 5 postal codes; 3 seats Hillsborough only.

 Standard Zone                     $497 / month                    $149 / sit                      3 to 5 postal codes; standard market pricing.

 Regional Territory                $797 / month                    $149 / sit                      8 to 12 contiguous postal codes.

 Market Lock (County)              $2,497 / mo min                 $149 / sit                      Full county buyout; complete exclusivity.


Exclusivity Mechanism: Tiers price territory, not features. Every tier receives identical screening, scoring, and dashboard access. Exclusivity is
enforced by database foreign keys mapping contracted postal codes to a single tenant identifier.

````

<a id="source-d-page-8"></a>

## Source D — Page 8

````text
6. Data Ingestion, Public Record Distress Signals & Sourcing

Ranked Owner Acquisition Signals
The acquisition engine ingests public records nightly, evaluating properties against six distress vectors ranked by historical conversion volume:

 Rank     Signal Identifier                                 Data Origin & Detection Rule

 1         Deed Transfer / Out-of-Area                      Warranty or quitclaim deed recorded where grantee mailing address differs from the property situs address (Highest
                                                            Volume).

 2         Homestead Exemption Removal                      County property tax assessor rolls indicate removal of primary residence exemption, converting asset to non-
                                                            homestead rental status.

 3         Residential LLC Formation                        New corporate entity registered with the Florida Division of Corporations (Sunbiz) utilizing a residential parcel as its
                                                            registered office.

 4         Portfolio Threshold Crossing                     Property owner crosses from 2 to 3+ residential single-family/multifamily units held within the county parcel
                                                            database.

 5         Probate & Estate Transfer                        Clerk of Court probate filings showing formal property distribution to heirs (Lowest Volume, Highest Close Rate).

 6         Out-of-State Ownership Shift                     County tax roll billing updates reflecting out-of-state mailing addresses.


Direct-Mail Address Binding: Public record deed and tax filings include the owner's legal tax billing address, enabling direct mailings without
secondary skip-tracing steps.

Data Sourcing Architecture

 Data Category                            Primary Sourcing Provider                          Technical Integration Architecture

 Statewide Corporate Data                 Florida Sunbiz (Div of Corp)                       Bulk weekly database dump + daily delta scraper.

 Deed, Tax & Parcel Data                  Regrid / ATTOM Data API                            REST API querying parcel boundaries & deed transfers.

 Building & Code Permits                  County & City Municipal Portals                    Targeted municipal scrapers for local building permits.

 Phone & Email Enrichment                 Hunter / Anymail / Tracerfy                        Batched API enrichment pipeline targeting top-tier scores.

````

<a id="source-d-page-9"></a>

## Source D — Page 9

````text
7. End-to-End Automated Outbound Marketing Pipeline

The outbound acquisition motion runs agent-to-agent to minimize day-to-day manual involvement.

 AUTOMATED OUTBOUND GROWTH TOPOLOGY

 [Raw Data Sweep: 500-1,000 PMs] ──► [Ghost-Shopper Inbound Bot] ──► [Speed & Revenue Loss PDF Compiled]
                                                                                    │
                                                                                    ▼
 [LinkedIn Profile Deep-Link] ◄── [Multi-Touch Cold Outbound Engine] ◄── [Sendspark Dynamic Video Generated]
                │                                   │
                │                                   ▼
                │                     [Inbound Reply Received]
                │                                   │
                │                                   ▼
                │                     [Reply Triage Intent Classifier]
                │                                   │
                └───────────────────► ┌─────────────┴─────────────┐
                                      ▼                           ▼
                              [Hot Lead Classified]     [Unsubscribe / Complaint]
                                      │                           │
                                      ▼                           ▼
                              [Setter Context Card]     [Deterministic Opt-Out Write]
                              [Generated in Slack]      [Suppressed Globally Across DB]
                                      │
                                      ▼
                              [Human Takes Call]



Automation Lifecycle & Mechanics
  Trigger: An automated ghost-shopper inquiry records a prospect firm's response time and compiles a personalized Speed & Revenue Loss Report.
  Delivery: The dynamic PDF report and personalized Sendspark video link are delivered via the primary outbound sequence.
  Follow-Up: The sequencer executes follow-up touches across email and LinkedIn tasks.
  Handoff Gate: Human involvement is triggered only when positive intent is detected by the Reply Triage Agent, generating a 1-screen context
  card for the closer in Slack.
  Compliance Boundaries: Sensitive replies, complaints, opt-outs, and closing negotiations remain strictly protected by deterministic rules.

````

<a id="source-d-page-10"></a>

## Source D — Page 10

````text
8. A2P 10DLC Carrier Messaging Compliance & Dependency

A2P 10DLC approval represents a hard operational dependency for appointment operations.

 10DLC OPERATIONAL DEPENDENCY CHAIN

                                      ┌──────────────────────────────┐
                                      │ A2P 10DLC Campaign Approval │
                                      │ (TCR / Carrier Clearance)    │
                                      └──────────────┬───────────────┘
                                                     ▼
                                      ┌──────────────────────────────┐
                                      │ Outbound SMS API Operational │
                                      └──────────────┬───────────────┘
                                                     ▼
                                      ┌──────────────────────────────┐
                                      │ 24-Hour & 3-Hour Reminders   │
                                      │ Dispatched & Logged to DB    │
                                      └──────────────┬───────────────┘
                                                     ▼
                                      ┌──────────────────────────────┐
                                      │ Programmatic Billing Gate    │
                                      │ Fulfills `confirmed` Checks │
                                      └──────────────┬───────────────┘
                                                     ▼
                                      ┌──────────────────────────────┐
                                      │ Verified Invoice Executed    │
                                      └──────────────────────────────┘



  The Carrier Clock: Carrier campaign vetting requires 7 to 21 business days.
  The Revenue Blocker: If 10DLC clearance is delayed, transactional confirmation text messages cannot be delivered. Without verified delivery
  timestamps in confirmation_logs , the appointment billing gate fails to evaluate green, blocking automated invoicing.
  Campaign Profile: Filed as Mixed: Customer Care + Account Notification (strictly non-promotional) with explicit opt-in verbiage tied directly to
  booking form submissions.

````

<a id="source-d-page-11"></a>

## Source D — Page 11

````text
9. Outstanding Implementation Items & Launch Checklist

The following items are consolidated across engineering briefs and represent critical pre-launch requirements:

 Component                          Current Status / Blocker                        Resolution Requirement

 Pull Requests #7 through #10        Pending Engineering Review                      Review and merge PRs into primary branch.

 DNC Scrubbing Integration           Currently Stubbed / Mocked                      Contract real-time DNC API vendor; integrate into pre-send pipeline
                                                                                     (Launch Blocker).

 Automated Test Coverage             Missing on Core Isolation Modules               Write unit/regression test coverage on quarantine_gate.py ,
                                                                                     promotion_sweep.py , county_allocation_reassessment.py .

 Domain DNS Verification             20 Domains Provisioned                          Publish verified SPF, DKIM, DMARC records and document operational
                                                                                     runbook.

 A2P 10DLC Registration              Submitted / Pending Carrier Review              Verify campaign approval; ensure fallback to email confirmations if
                                                                                     delayed.

 Stripe Catalog Configuration        Seven NULL plan_price Rows                      Seed database with complete price rows including
                                                                                      owner_growth_founding ($397/mo + $99/sit).

 Preflight Integration QA            15-State Checklist Automated Tests              Validate end-to-end roundtrip lead injection and automated dispute
                                                                                     logging.

````
