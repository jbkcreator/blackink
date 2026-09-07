# TASK: audit the repo for a legacy campaign-readiness gate and remove it

Label: `wayfinder:task`
Status: open
Assignee:
Blocked by: —

## Question

Small, possibly zero findings — a grep before a decision.

The blueprint's §2.1 requires:

> "**Remove** the old readiness prerequisite requiring a ghost-shopper score
> or video ID. Both underlying production features are held; **otherwise the
> old gate would block all legitimate September campaigns**."

And the Source of Truth's code cautions (§5.2) name the specific artifact:

> "`evaluate_campaign_readiness` lives under a Python-looking path in the
> document but is SQL/PLpgSQL. It **requires held audit/video inputs and uses
> old field names**. Preserve text; adapt deliberately before implementation."

Source path in the blueprint: `src/compliance/gate_evaluator.py`
(Blueprint p2). A `001_core_spine.sql`-era "Ready for Campaign" gate also
appears in the v2 blueprint's §1 ingestion section.

**Check whether this repo has a port of it**, in any form:

- `grep -rn "evaluate_campaign_readiness\|campaign_readiness\|ready_for_campaign" --include=*.py --include=*.sql .`
- Any Postgres function doing the same job:
  `SELECT proname FROM pg_proc WHERE proname ILIKE '%readiness%' OR proname ILIKE '%campaign_ready%';`
- Any check in `quarantine_gate.py` or `promotion_sweep.py` gating promotion
  on audit or video presence.
- `assert_audit_complete` in `src/services/audit_report.py` — the known
  instance. Ticket 11 already rules it must come out of the dispatch path;
  confirm nothing else calls it.

**If found:** remove it from the dispatch and promotion paths. Do not rebuild
it on the Owner Visibility Score — §2.1 says remove, and the score is Touch 1
*content*, not a gate (ticket 11).

**If not found:** close with "no port exists", which is a useful negative
result — the blueprint's printed SQL was never implemented here.

## Why it matters

If a port exists and still requires audit or video inputs, it will block
**every** September campaign, silently and at the last moment — both inputs
are cancelled. Cheap to check now.
