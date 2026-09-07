# Define `template_version`

Label: `wayfinder:grilling`
Status: closed
Assignee: —
Blocked by: —

## Resolution (2026-09-03)

### The prior question is answered by the code: copy is LLM-generated

`config/prompt_variants.py` holds champion/challenger **system prompts** that
Cora renders per prospect; `outbound_templates.py` validates merge tags in the
**rendered output** (`ALLOWED_TAGS`, `REQUIRED_TAGS={client_firm}`,
`validate_template`, `resolve_tags`). So it is generation, not a fixed
template — matching §23's "Cora drafts dynamic sequence payloads".

### `template_version` = the variant `name`

Use the existing `name` field verbatim: `t1_v1_speed_evidence`,
`t1_v2_loss_lead`, etc. Reject the blueprint's `v1.4_speed_audit_video` shape
— it is illustrative, and its slug names two cancelled features.

Source the string from **one function**, not a literal at each call site, so
the value in the event payload cannot drift from the variant actually used.
`get_champion_prompt(touch)` already selects the variant; have it return the
name alongside the prompt rather than making the caller re-derive it.

Champion vs challenger is then distinguishable in the digest for free, since
they are different names.

### ⚠ Both Touch 1 variants are dead content

`_TOUCH_1_VARIANTS` is built entirely on cancelled material:

- `{audit_speed}` — ghost-shopper latency, **prohibited** (§1.8)
- `{video_url}` and "the Sendspark video" — **held** (§1.5)
- `{loss_dollars}` — was ghost-shopper-derived; the loss model is now 8% fee /
  30-month tenure / ~$100 per door per month (§1.8), and §1.8 warns "do not
  reuse an unobserved ghost-shopper latency as though it were measured"
- "industry 1-hour benchmark" in `t1_v2` — a speed claim with no permitted
  source

Both need rewriting against the Owner Visibility Score. That is ticket
[19](19-owner-visibility-score-touch-content.md)'s work; flagged here so the
rewrite is not missed when someone reads only this file. Touch 5's variants
need the same treatment (Metro Speed Index and seat scarcity are both gone).

Also stale, harmlessly: the module docstring says merge-tag syntax is
`{single_brace}` "(Instantly)". Instantly is eliminated as the sender
(ticket 04). Keep the syntax; drop the rationale.

### The gap `template_version` cannot close: human revision

The approval workflow (ticket 21) includes a **Revise** action — the listeners
already implement approve/reject/snooze/revise. So a human can edit a draft
before it sends, and `template_version` then names the **generator**, not what
actually went out. Attributing reply-rate to `t1_v1` when a human rewrote the
body measures the wrong thing, and silently.

**The ground truth already requires the fix.** §2.7's eight memory categories
include "counterfactual/**revision reasons**" and an "**experiment
registry**". So revision tracking is a named requirement, not extra scope.

Minimum: carry `was_revised` (bool) in the `outbound_touch_dispatched`
payload alongside `template_version`, and capture the revision reason where
§2.7 wants it. The digest must be able to exclude revised sends from
champion/challenger comparison, or the experiment is contaminated.

### Do not confuse the two promotion models

- **Variant promotion** (this module's docstring): "≥60 sends, ≥2pp absolute
  reply-rate lift over champion, and founder approval" — an A/B copy decision.
- **Autonomy band promotion** (§2.7): Band 2 at 50 clean approvals / ≥95%
  approval; Band 3 at 250+ clean / <2% dispute — a *trust* decision about
  whether a class may dispatch without review.

Different things, similar-looking thresholds, easy to conflate. Decision
register **O-08** already flags band naming as needing reconciliation; do not
let variant promotion get folded into it.

Note also `golden_set_approved` gates on `golden_set_eval`, which the
docstring says is Sprint 2 and not yet built — so today only the champion is
genuinely eligible to send.

## Question

`template_version` is a **mandatory** payload key on every
`outbound_touch_dispatched` event (§8, §27.2). Omit it and the event is
rejected. But no `template_version` concept exists anywhere in the repo.

The nearest analogue is `config/prompt_variants.py`, whose champion/
challenger variants carry names like `t1_v1_speed_evidence`. The blueprint's
own example uses `"v1.4_speed_audit_video"` (§8.1) — a different shape
entirely.

Decide:

- **What is it a version of?** The LLM prompt variant that generated the
  copy, or the rendered template/layout, or both? These diverge the moment
  the same prompt is used with a changed layout.
- **What is the string format?** Ratify either the variant-name shape
  (`t1_v1_speed_evidence`) or the blueprint's `vN.N_slug` shape, and make
  the source of the string a single function rather than a literal at each
  call site.
- **Who bumps it, and when?** If a human edits copy without bumping, the
  metric silently attributes new copy to the old version.
- **How does it interact with champion/challenger?** `get_champion_prompt`
  selects a variant at send time. If the digest is to compare champion
  against challenger, `template_version` must distinguish them — confirm the
  variant name is actually reaching the event payload, and that a challenger
  send is distinguishable from a champion send.

Note: `prompt_variants` holds **prompts**, not rendered copy. So there is a
prior question — is Touch 1/3/5 body copy LLM-generated per prospect at send
time, or is it a fixed template with merge tags? §23 has Cora "drafting
dynamic sequence payloads", which implies generation; `outbound_templates`
implies merge tags. Settle that first, because `template_version` means
something different in each world.

## Scope note

Small in isolation, but the prior question it exposes — generated copy vs
merge-tag template — is not small, and may deserve its own ticket.
