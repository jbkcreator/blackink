# Design the every-send approval workflow

Label: `wayfinder:grilling`
Status: open
Assignee:
Blocked by: —

## Question

The sequencer cannot autonomously dispatch. The Dev 3 reference never says
this; the Source of Truth does, twice:

> "Apply the comments' behavioral rule first: **every early-client send
> reviewed**; class-specific earned authority; no agent discretion over
> legal/financial policy." — §2.7
>
> "**Start with human approval for every send.** A template class may earn the
> tighter promise after **50 clean approvals**, when the behavior can be
> proved." — §1.6

Decision register **O-08** flags the conflict — "'Band 2 from launch' versus
every-send review at first client" — and rules that "explicit later
early-client correction controls." So: every send reviewed, until a template
class earns otherwise.

Settle:

1. **Where does approval happen?** `agent_work_orders` already carries
   `autonomy_band`, `risk_class`, `status`, `execution_receipt`, plus Slack
   `channel_id`/`message_ts`, and the Slack layer already has hash-bound
   approve/reject/snooze/revise listeners. This is a strong argument for
   routing touches through work orders — and it substantially decides
   ticket 06. Confirm, or say why not.
2. **What is the approval unit?** One card per email is faithful but
   brutal: a 100-address campaign (acceptance Link 2) means 100 approvals for
   Touch 1 alone. One card per batch is humane but "every send reviewed"
   arguably means every send. Is a batch card showing N rendered emails with
   one Approve-All acceptable, or does each need individual judgement?
   **Ask the client** — this is a promise about review, not an ergonomics
   choice.
3. **Counting toward the 50.** "50 clean approvals" per **template class**
   promotes it. Where is that counter, what is a "class", and what resets it?
   §2.7's fuller band table says Band 2 needs 50 clean / ≥95% approval and
   Band 3 needs 250+ clean / <2% dispute, "with fault/policy demotion" —
   so demotion must exist too, not just promotion.
4. **What happens to a scheduled touch awaiting approval?** Touch 3 is due
   Day 4. If nobody approves for two days, does it send late, get skipped, or
   halt the sequence? This interacts directly with ticket 06's scheduling and
   ticket 07's state model — an approval queue is a second source of delay
   the cadence has to tolerate.
5. **Who approves?** Decision register **O-07**: "business hours, timezone,
   holidays, approver fallback" are missing. §2.2 notes "a channel inventory
   is not a named approval owner or fallback roster." An approval workflow
   with no named approver and no fallback stalls the whole sequence the first
   weekend.
6. **Wire `notify_approval_resolved()` — it is ours and it is missing.**
   `src/agents/cora/throttle.py:20` assigns the call site to us explicitly:
   "Dev 3 (Slack bot) calls `notify_approval_resolved()` when an operator
   clicks Approve/Reject on a draft card." It has **no caller anywhere in
   non-test code**. `notify_draft_queued()` *is* called (`worker.py:64`), so
   `cora:approval:pending` only increments — Cora auto-pauses at
   `DRAFT_QUEUE_CAPACITY = 50` and **never resumes**, because nothing
   decrements it toward `RESUME_THRESHOLD = 40`.

   This ticket owns wiring it into the Slack approve/reject listener. Note
   the counter is deliberately **not tenant-scoped** (`throttle.py:16-18`) —
   a global platform resource for Week 0. With every-send review, the backlog
   will hit 50 quickly, so this is not a latent bug; it will fire on the
   first real campaign.

   The other half of W0-1 — the **24-hour aged-draft trigger** — is Dev 2's
   gap, tracked in ticket [22](22-execution-lease-double-send.md) as Bug C.

## Why it matters

This changes the sequencer from a dispatcher into a drafting-plus-approval
pipeline. It is the single largest architectural consequence of the Source of
Truth for Task 3.1.1, and it lands on ticket 06 before ticket 06 is decided.
