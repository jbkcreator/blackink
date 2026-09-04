# What do Touches 1 and 5 actually say now?

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03) — `Week1_Tasks_Dev_Split_v2.md`

**Question 5 — who builds the score — is answered, and it is not the
September blocker this ticket feared.**

**Dev 2 owns it**, as a whole workstream: Task 2.1 "Owner Visibility Score
Engine & Report Generation", with subtasks 2.1.1 (public signal scraper +
score calculator), 2.1.2 (county rank + peer benchmarking), 2.1.3 (score PDF
report compiler). Dev 2 also owns Task 2.2, the Fee-Stack one-pager.

So Dev 3 **consumes** two artifacts and builds neither.

### What each touch now says

| Touch | v2 content |
|---|---|
| 1 | Owner Visibility Score **PDF attached**; micro-ask "Reply YES to see where you rank in **[County]**". Explicitly **no GIF thumbnail, no video link** |
| 3 | **Fee-Stack One-Pager attached**; threaded to Touch 1 |
| 5 | **County Visibility Rank & Scarcity** |

**Correction to this ticket's premise:** it assumed the scarcity angle died
with the metro layer and with county seats leaving September. It did not —
v2 keeps scarcity, rebased on **county visibility rank** rather than seat
availability. Question 3 ("what is the reason to reply on Day 10?") is
answered: the prospect's county rank.

**Question 1 (presentation order) is also settled for the *report*** — v2
2.1.3 requires the PDF to show score, county rank, data coverage percentage,
named peer comparisons, and model assumptions. Email copy order is not
specified and remains a copy decision, not an architecture one.

**Question 2 (the top-25 problem) survives.** Rank is publishable for the top
25 per county only, yet Touch 1's micro-ask promises "where you rank in
[County]" and Touch 5's whole angle is rank. For a firm ranked 40th there is
no publishable rank. v2 does not address it. → CLIENT-ASKS **D16**.

**Question 6 (video trigger hook)** — still required: a hook plus a disabled
provider row, nothing more. Keep it vendor-generic.

### What Dev 3 must confirm with Dev 2

- The **interface** for fetching a contact's score, county rank, and three
  lowest-scoring categories — Touch 2's card needs all three (v2 3.1.2).
- **PDF retrieval**: path, URL, or bytes, and whether generation is
  synchronous at send time or pre-generated.
- **Availability semantics**: what Dev 3 receives when a firm is below the
  data floor or unscored. Ticket 11 covers the sequencing consequence.
- The Fee-Stack one-pager's equivalent interface for Touch 3.

## Question

The Source of Truth removed the proof that Touches 1 and 5 were built on.
Something has to replace it, and nothing in the repo or the Dev 3 reference
says what.

**What was removed:**

- Touch 1's Speed Loss Audit — "**Do not build the ghost shopper or submit
  pretext inquiries**" (§1.8); "do not activate deprecated ghost-shopper
  calculations" (§5.2)
- Touch 1's Sendspark video and GIF — held for September (§1.5)
- Touch 5's Metro Speed Index — no metro layer (§1.1)
- Touch 5's territory-lock scarcity — county seats are out of September
  (§1.9), and §2.3 names "active seat scarcity" as a claim to replace

**What replaces it** (§2.3): "initial proof email … final county/public-
observation angle", drawing on "permitted public evidence and actual enabled
offers."

The permitted evidence is the **Owner Visibility Score** (§1.8): 100 points
across owner-conversion readiness (separate owner page 14, working contact
form/phone/email 10, mobile+HTTPS+<3s 6), market visibility (review count vs
county median 16, review recency 12), public reputation (response rate 18,
average rating 8), accessibility (after-hours route 8, median response lag 4),
and credibility (active broker licence and tenure 4). Sources are business
profiles, the firm's website, and state licence rolls.

Settle:

1. **Touch 1's angle.** §1.8's presentation rule — lead with the three
   lowest-scoring observations and named comparisons, then score, then rank —
   was written for the *report*. Does the *email* follow the same order? A
   cold email opening with three criticisms of the recipient is a different
   proposition from one opening with a rank.
2. **The top-25 problem.** Rank is publishable only for the top 25 in a
   county, and there is never a bottom list. Most recipients are therefore
   unrankable in copy. Does Touch 1 fork into a ranked variant and an
   unranked variant? That doubles the template set and interacts with
   `template_version` (ticket 12).
3. **Touch 5's angle.** "County/public-observation" is a category, not copy.
   With scarcity and the Speed Index both gone, what is the actual reason to
   reply on Day 10? This is the weakest-specified touch in the sequence.
4. **The loss model.** §1.8 sets defaults of **8% management fee, 30-month
   average owner tenure, ~$100/door/month**, stored per client, assumptions
   shown, labelled as estimates, replaceable with client actuals without
   deployment. Confirm this is what the email's dollar figure is computed
   from, and that it is never presented as measured.
5. **Who owns the score computation?** It needs business profiles, website
   crawls, and state licence rolls. That is a data-acquisition workstream and
   almost certainly not Dev 3's. Is it Hunter's? Does it exist? Touch 1
   cannot fire without it, so if it does not exist this is the real
   September blocker, not anything in the sequencer.
6. **The video trigger hook.** §1.5 requires "a video trigger hook and
   disabled/configured provider row only". Define the hook's call site and
   the provider row's shape — generic, not Sendspark-specific, since no
   vendor is committed.

**Do not invent the scoring formula.** Decision register **O-14**: category
weights are supplied but "scoring interpolation/data floor absent — do not
invent normalized formula, missing-data denominator, tie-breaks or licence
treatment." If Touch 1's copy needs a number the formula cannot yet produce,
that is a client ask, not an implementation detail.

## Why it matters

Everything else on this map is plumbing. This is the message. Touch 1 cannot
be built, and the September 11 demo cannot be run, until someone decides what
the email says and where its proof comes from.
