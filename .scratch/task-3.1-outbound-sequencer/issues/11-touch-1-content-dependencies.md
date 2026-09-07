# Establish readiness and ownership of Touch 1's content dependencies

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: — (19 closed)

## Resolution (2026-09-03) — `Week1_Tasks_Dev_Split_v2.md`

**The report is a PDF attachment, and Dev 2 builds it.** Subtask 2.1.3 is the
"Owner Visibility Score PDF Report Compiler"; Touch 1 "dispatches Email 1
containing the Owner Visibility Score **PDF attachment**". So:

- The PDF-vs-hosted-page question is **closed: attachment.** This also
  confirms ticket 05's sender must support attachments — which it does, now
  that dispatch is SMTP from real Workspace/Outlook mailboxes.
- **No renderer for Dev 3 to build.** `audit_report.py`'s "Week 2" section
  builders are not on Task 3.1's path.
- Touch 3 attaches the **Fee-Stack one-pager** (Dev 2, Task 2.2).
- Explicitly **no GIF, no video link** in Touch 1.

**The readiness gate: remove, do not rebuild.** Blueprint §2.1 —
"**Remove** the old readiness prerequisite requiring a ghost-shopper score or
video ID… otherwise the old gate would block all legitimate September
campaigns." `assert_audit_complete` comes out of the dispatch path. The score
is Touch 1 *content*, not a *gate*.

**Recipient eligibility remains the real blocker, and v2 does not solve it.**
Touch 1 needs a verified corporate email; the gate requires
`email_status = 'VERIFIED'`; `promotion_sweep.py:214` writes `"UNVERIFIED"`;
nothing ever sets `VERIFIED`; `evaluate_compliance_gate`'s `email_provider`
parameter is accepted and never used. v2's shared DoD needs a campaign to
**100 verified addresses**. Still CLIENT-ASKS **A13**, still blocking every
send.

**Interfaces to agree with Dev 2** (carried to ticket 19): how Dev 3 fetches
the PDF, the score, the county rank, the three lowest categories, and what
comes back for an unscored or below-data-floor firm.

**Copy detail:** the micro-ask is "Reply YES to see where you rank in
**[County]**" — county display name, per closed ticket 13.

> **Rewritten 2026-09-03.** The original version of this ticket asked about a
> ghost-shopper Speed & Revenue Loss PDF and a Sendspark GIF. The Source of
> Truth cancels both (§1.8 "do not build the ghost shopper", §1.5 video held).
> The previous text is superseded — see git history if needed.

## Question

Given that Touch 1's proof is now the **Owner Visibility Score** (ticket 19
defines what the email says), what does the sequencer actually need to have
in hand before it can dispatch, and who supplies it?

**The report artifact.** §1.8 requires the report to display the score, its
**data coverage** ("76/100, 94% data coverage"), the three lowest-scoring
observations with named comparisons, then the county rank. Settle:

- Is the report a **PDF attachment**, a **hosted page**, or both? The old
  design attached a PDF. A hosted page is cheaper to build, is trackable as a
  click, and dodges the attachment-support constraint on the sender
  (ticket 05) — but "attachment" was the DoD wording, and a link is weaker
  proof in a cold email.
- If hosted, on what domain? §1.5 protects `getblackink.com` as the brand
  domain and reserves `watch.getblackink.com` for future video. A report URL
  on a cold-outreach domain looks like spam; one on the brand domain mixes
  reputation. Decide deliberately.
- `src/services/audit_report.py` has two PDF **section builders** marked
  "Week 2" and **no renderer**. Does a renderer get built, and by whom?
  If it must run in the container, check the engine works there — that is a
  routine Docker surprise.

**Score availability as a sequencing gate.** `assert_audit_complete`
currently gates on a ghost-shopper audit event. Its replacement gates on a
computed Owner Visibility Score. So:

- Can Touch 1 fire for a contact whose score has not been computed? Skip,
  defer, or error?
- §1.8 says below the data floor, show "insufficient data" and **no rank**.
  Is such a firm mailable at all? A cold email whose entire proof is "we
  couldn't find enough data about you" is not proof. Likely answer: exclude
  from the campaign — but that is a real ICP filter and should be explicit.
- §1.8: publish **top 25 per county only, never a bottom list**. If a firm
  ranks 40th, Touch 1 cannot tell it its rank. What does it say instead?
  This directly shapes the Touch 1 template and may split it into two
  variants.

**Recipient eligibility.** Dev 3 ref §4.2 says Touch 1 goes to a **verified
corporate email only**, and the gate requires `email_status == "VERIFIED"`.
Nothing sets VERIFIED — `promotion_sweep` writes `"UNVERIFIED"` and
`evaluate_compliance_gate`'s `email_provider` parameter is **accepted and
never used**. Link 2 of the acceptance contract (§1.12) requires a campaign to
**100 verified addresses**, so this is on the September critical path.

- Who verifies? §4.2 of the Source of Truth names Hunter/Anymail/Tracerfy as
  providers but warns "the attachments do not prove that API contracts,
  permission, coverage, or credentials exist."
- Is verification a promotion-time step or a send-time step? Same shape as the
  DNC decision already taken (scrub at promotion).

**Also carried over, still live:** the `{city}` merge tag is being repointed
to county (ticket 13, closed) — confirm Touch 1's copy uses the county
display name, not the slug.
