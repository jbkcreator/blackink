# Client Responses Log

Verbatim-faithful record of client responses/decisions, newest first. Captured
for the team; reformatted into clean tables but wording preserved. Not all of
this is Dev 3 / Task 3.1 scope — items are cross-team.

---

## 2026-09-04 — Pricing, Signals, Inbound/Reactivation, Referral Partners, Owner-facing & Demo, Billing

### 1 · Pricing — FINAL (replaces previous item)

Three SKUs. Every one a founding rate. **Retire everything not listed.**

| Row | Price | Notes |
|---|---|---|
| **Respond** | $397/mo | Founding rate |
| **Owner Growth** | $749/mo + $99 per attended qualified sit | Founding rate. $897 flat stays as a row, not default |
| **Full County** | $1,197/mo + $99 per attended qualified sit | Founding rate. Exclusive — one per county, enforced |

**Config rules**

- Every row prints **"Founding rate — normally higher"** on the offer sheet. **No list price stored or shown anywhere.**
- **Retire:** $297 Respond, Scorecard $449 tier, additional-county add-on, and the founding row asked for "last night".
- **No monthly ceiling on billable attended sits.** If a cap exists anywhere in the billing gate, **remove it**.
- **Full County** is an entitlement row **plus** a county-level exclusivity lock in the allocator — one `full_county` flag per county, second assignment rejected. Owner Growth accounts in that county keep their ZIPs until renewal.
- `founding = true` on the account row. When list prices change later, founding accounts never move. One flag, checked by the billing job.
- **Demo kit config:** Respond $397, Owner Growth $749 + $99, Full County $1,197 + $99 — **not** $397 + $100.

_Vendor screens stay September as agreed. Realtor UI is Q1. Realtor backend is September._

### Signals and sourcing

| Item | Spec | Hrs |
|---|---|---|
| **Homestead exemption dropped** | Assessor flag. Owner moved out, property stayed. Add to the deed engine as a first-class signal with its own row in the Owner Packet | 2–3 |
| **Same-owner assessor match** | Input: the client's managed-address list (CSV or PMS export — address, owner name). Group the assessor roll by owner mailing address and entity; surface parcels those owners hold that aren't on the client's list. Output: twelve addresses per run, exportable, shown on Screen 1 | 12–20 |
| **Whale ranking + out-of-state tier** | Parcel count on every owner row. Priority tier where mailing state ≠ FL. Sort order in the nightly sweep and the packet | 4–6 |
| **Win-back import** | CSV of owners the client lost. Match to assessor (still owns?) and FRBO feed (still renting?). Disposition column | 4–6 |
| **Direct mail merge** | Absentee rows (mailing ≠ property) → print API with rent estimate and client name. Batch, not per-row. Use Lob or PostGrid — don't evaluate, pick one | 4–6 |
| **HOA rental-cap flag** | Cross-reference condo/HOA lists for rental caps and waiting periods before an appointment can be booked | — |

### Inbound and reactivation

| Item | Spec | Hrs |
|---|---|---|
| **Six-attempt inbound cadence** | After the 60-second ack, five further attempts over five days if the owner goes quiet. GHL sequence, stops on reply | 3–5 |
| **Dead-lead 9-touch SMS drip** | Nine touches over 30 days to the client's dead-lead file. From the client's 10DLC number, in the client's name, referencing their original inquiry. Fires only where `phone_consent = true` on the contact row. Email pass runs first. Quiet hours 8am–8pm ET. STOP propagates to the consent ledger and halts every sequence. Any reply exits the drip to a human | 4–6 |
| **Post-sit nurture** | Sits dispositioned "thinking" or "not now" enter a 45-day sequence in the client's name: day 2, 7, 14, 30, 45. Stops on reply. Setter task fires at 14 and 45 | 8–12 |
| **Review-velocity trigger** | Google review request at onboarding and day 90, routed to the client's GBP | — |

### Referral partner system — end to end, September

| Item | Spec | Hrs |
|---|---|---|
| **`referral_partner` table** | Reuses the vendor table shape. Fields: `type` (agent / attorney / insurance / lender / CPA / HOA-CAM / trade / other), ZIPs, criteria card (JSON), tier, `last_sent`, `last_report_back` | 3–4 |
| **`sell_intent` disposition** | New outcome value. Out-of-criteria seller signals are tagged, not closed — they stay in the pool | 2–3 |
| **`signal_surfaced_to` event** | Records that a signal was shown to a partner. **Never an "introduction" event.** Timestamped | 2–3 |
| **ZIP-match routing** | Seller signals and sell-intent dispositions route to partners whose ZIPs and criteria match. Live-tier → same day, individually. Public-record tier → weekly batch | 4–6 |
| **Monday digest** | Assembled Sunday night, drafted to my inbox for approval, one email per partner, five to eight ranked rows. **Never auto-sends** | 4–6 |
| **48-hour report-back** | Trigger 48 hours after any live-tier send. One-line status field on the event. Reminder to me if empty | 2–3 |
| **Partner scoreboard** | Per partner: sent, contacted, closed, junk. Screen or export — export is fine for September | 2–3 |

### 6 · Owner-facing and demo

| Item | Spec | Hrs |
|---|---|---|
| **Rent-estimate landing page** | Address in, estimate out, booking under it. Client-branded from config. Estimate from RentCast API; page on GHL funnels, not custom | 4–6 |
| **Ryse eligibility flag** | Fires only for properties under the client's management. Owner-facing copy states eligibility begins at signing. 2% to client on every advance | 6–10 |
| **ZIP-level landing pages** | Templated from the zone map. One page per ZIP, client-branded, schema markup. GHL funnels or a static generator — whichever is faster | 6–10 |
| **Pixel + UTM plumbing** | Meta and Google pixels on every page; `raw_first_source` populated from UTM | 3–5 |
| **Live inbound trigger in the demo** | Screen 5 button fires a form-fill or call to the client's own listing line while screen-sharing. Logs the response time to the ledger | 1–2 |
| **Pay-per-lead routing** | Parse inbound from All Property Management, Manage My Property, Thumbtack into the Respond queue | 4–8 |

### 6b · Billing rules the guarantee depends on

Rules, not screens. Most are a few lines in the billing job. Without them the offer sheet makes promises the system can't keep.

| Rule | Spec | Hrs |
|---|---|---|
| **$50 miss credit** | Any Respond inbound not acknowledged inside 60 seconds writes a $50 credit line to the client's next invoice. Automatic, no approval. Miss and credit both visible on the proof ledger | 2–3 |
| **First sit free** | First attended = true sit per account bills at $0 | 1 |
| **60-day guarantee** | If an Owner Growth or Full County account has fewer than four attended qualified sits at day 60, the next month's subscription bills at $0. One-time | 2–3 |
| **48-hour dispute** | Already in the 18 Sept ledger — confirm a disputed sit is credited **on dispute, not on resolution** | 0–1 |
| **Skip-trace on surfaced owners** | Confirm the enrichment step (owner → phone/email) for every signal, including the new homestead and same-owner rows. If it isn't, it's a **blocker for everything in §3** | confirm |

### Open asks from the client

- **Share vendor names** to account for:
  - **DNC Scrub**
  - **Skip-trace**
