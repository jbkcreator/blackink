# ***Clarifications Before Implementation***

---

---

# Week 0 - Open Items

## 1. Cora Draft Queue - Pause Threshold

The backpressure fix needs a queue-size limit before drafting auto-pauses.

**Question:** What number of unreviewed drafts should trigger a pause? (A rough starting number is fine.)

**Answer:** 50 unreviewed drafts, plus an independent trigger on any draft older than 24 hours. Two triggers, because a count alone misses a slow leak. The defect is that the pause must stop generation, not just sends. Acceptance: queue at 50 → no new drafts and the requeue stops · one Slack notice, not one per blocked attempt · clean resume with no duplicates or lost items · an aged draft pauses generation even at low count.

## 2. Vera Silent-Zero Fix - Scope

The fix should show \"unknown\" instead of a false \$0 when data is missing. The implementation can be narrow or broad.

**Question:** Should this cover only revenue/billing reports, or should the silent-zero behavior be audited across the whole system?

**Answer:** Narrow fix now. Fix revenue and billing properly (the 8 hours budgeted) · make UNKNOWN/ABSTAIN a first-class return type in Shared Agent Core — if only one of these happens, make it this one · time-box 2 hours to sweep and hand back a severity-ranked list, nothing else fixed silently inside the 8.

**3. Hunter Standalone Worker - Infrastructure**

Week 0 calls for Hunter to operate as a standalone worker.

**Question:** Can we provision a new Hetzner instance for Hunter\'s standalone worker?

**Answer:** Yes, provision it. One check first: Hunter/Scout already runs on its own droplet for the 807K-entity resolution — confirm migration or second box, because two resolvers with drifting data is worse than the problem. If migration, the nightly-sweep proof runs on the new instance.

## 4. Blackink Repository & Server Provisioning

The blueprint describes Blackink as its own operating platform but does not clearly specify the repository or hosting environment.

**Question:** Please confirm whether **Blackink** should have a separate dedicated repository from the existing codebase. If yes, please also create and share GitHub repo under your organization. Also, can we provision a new dedicated server instance for Blackink? **This is urgently required for both development and production database setup.**

**Answer:** Dedicated repo yes, dedicated dev and production yes. READ THIS TWICE: a separate repository is not a separate codebase. Blackink uses the Forced Action front end as its base and reuses Cora, Vera, Hunter and Relay. It is a fork with a reuse ledger, not a greenfield build. If you have read it the other way, tell me this week — that reading does not fit in one month at any headcount. Provision production before the outcome table is written · no production PII in dev or staging · nightly backup from day one and one restore actually tested before the first paying client.

# Week 1 - Open Items

## 1. Target Company List & Akrash Responsibility

Week 1 depends on the initial property-management prospect list being available and on a clear ownership boundary for upstream sourcing/enrichment.

**Question:** Please also confirm that Akrash is responsible for sourcing/enriching those firms and their two required contacts, while our responsibility is to provide the raw_prospect_pipeline staging interface and process/validate the submitted records.

**Answer:** Confirmed as you described. Build the interface to a spec, not to a file — publish the schema and validation rules now, accept the first batch whenever it lands. Minimum schema: firm_name · website_domain · county · door_count_estimate · door_count_source · two contacts (name, role, email, phone, source) · enriched_at · submitted_by · validation_status · reject_reason_code. Rejected rows return with a reason code; nothing dropped silently.

## 2. Sendspark Account

The personalized video flow depends on Sendspark access and the Blackink video domain being configured.

**Question:** Has a Sendspark account been created, and is watch.blackink.io domain set up?

**Answer:** No account yet, and watch.blackink.io cannot be set up — we do not own blackink.io. getblackink.com is the protected brand and identity domain; the rest are cold-outreach only. Video, if hosted, goes on watch.getblackink.com. Do not build the video flow in September — build the trigger hook and leave the provider behind a config row.

## 3. UI / Frontend Design Requirements

The blueprint defines the workflows and functional requirements, but does not provide a complete UI/UX design specification for the Blackink application.

**Question:** Do you have any existing Figma/designs, branding guidelines, reference products, or specific UI requirements we should follow? If not, should our team design the UI based on the functional requirements in the blueprint? Once the expected UI scope/design direction is confirmed, we can provide a more accurate frontend development timeline.

**Answer:** No Figma, no branding guideline, and none needed. Take the Forced Action components, layouts and patterns. Where there is no precedent, design to the functional requirements — no review cycle, no mock-up round, I give feedback on the running thing. The one surface worth real care is the daily approval screens. Quote the frontend against reuse; a greenfield number is the wrong number.

## 4. 10DLC Registration --- Landing Page Requirement

**Blocker:** For the Telnyx A2P 10DLC registration, we need a valid public Blackink landing page, which is currently not available.

**Question:** Please provide the requirements/design expectations for the Blackink landing page. Once the requirements are confirmed, we will review the scope, provide the estimated development timeline.

**Answer:** It is the rank report's front door, not a separate site, and it is already inside the build. In the order carriers check: hosted on getblackink.com · HEU AI LLC, 971 US Highway 202N Ste N, Branchburg NJ 08876, working contact email visible · Privacy and Terms that resolve · a form collecting a mobile number with the consent disclosure visible at the point of collection · a plain-language description consistent with Customer Care and Account Notification, not marketing. Ship the minimum version, submit immediately, improve while queued. Not a blocker — we send no SMS this year.

## 5. Rent Valuation API

The Rent Analysis Bot requires a live rent-valuation provider.

**Question:** Is it CoreLogic or RentCast - which one do we actually have a contract with?

**Answer:** Neither. No contract exists with either provider, so the Rent Analysis Bot cannot be built — it is already Q1 for that reason. Define the adapter interface only if close to free, leave the provider row disabled. Do not start a trial on our behalf.

## 6. Ghost-Shopper Script

The ghost-shopper flow needs an approved inquiry message before it can be used against real property-management websites.

**Question:** The blueprint requires the Ghost-Shopper Agent to submit a standardized owner inquiry through target PM websites, but the exact message is not defined. **Should we draft the standard inquiry message for approval, or is there already approved wording we should use?**

**Answer:** Hold. Do not build it. Every other item on your list is an engineering question; this one asks us to submit an inquiry to a real business under a pretext, at volume, then publish and sell a ranking built on it. Rank v1 runs on public observable data only. Methodology we can publish when a ranked firm asks how we got their number.

## 7. Loss-Report Numbers

The Speed & Revenue Loss report requires business assumptions for its calculations.

**Question:** What is the real average management fee and average number of years an owner stays that we should use in the audit reports?

**Answer:** 8% management fee · 30-month average owner tenure · ~$100 per door per month. These are per-client config rows, not constants — a firm charging 10% will reject a national assumption. Every report states its inputs on the page. Straight with you: these are estimates, not observations. The first real client's actuals replace the defaults, and that must not require a deploy.

## 8. Calendly Setup

The booking flow needs a confirmed calendar/account structure before implementation.

**Question:** Can the current Calendly plan handle per-client booking, or do we need a different setup?

**Answer:** The premise is off and it is cheaper than expected — we need no Calendly plan for the product at all. Blackink never books into a Blackink calendar: Launch has calendar-connect as an explicit gate and the attendance bar requires both parties on the client's own event with a proof_ref. Integrate against Google Calendar and Microsoft Graph; GoHighLevel is the fallback for a client with no connectable calendar. If you sized this against the Calendly API, please re-size it.

**9. Sales Demo Bot --- Vendor Consolidation**
The plan uses Twilio for the demo bot and Telnyx for real customer messaging --- two separate companies for two phone numbers.
**Question: Can the demo bot just use a second Telnyx number instead of a separate Twilio account, or is there a specific reason it needs to be on Twilio?**

**Answer:** Yes, consolidate on Telnyx — one carrier, one credential set, one CDR source. THE CONDITION: the demo bot must not run on the production 10DLC campaign. That campaign registers as Customer Care and Account Notification. A demo bot messaging a PM prospect fits neither and risks the registration the product depends on. Second Telnyx number, separate campaign registration, demo messages only after the prospect texts in first. A "text DEMO to this number" flow makes the consent clean and is a better demo than an unsolicited text.
