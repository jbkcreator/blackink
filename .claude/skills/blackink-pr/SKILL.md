---
name: blackink-pr
description: Branch + open a PR for finished work in this repo — but only after the blackink-review skill has actually run this session and passed. PR description is structured as Done / Remaining / How to finish. Use when the user asks to open a PR or ship a task.
---

# Open a PR (Blackink)

This skill's whole point is sequencing: **no branch, no push, no PR until
`blackink-review` has run in THIS session against the current diff and come
back clean (or with findings the user has explicitly accepted).** Running
it for real — actually reading the current diff, section by section, the
way its own instructions say to — is exactly what this gate wants, whether
that happens earlier in the conversation or as a step you invoke right now.
The anti-pattern isn't "running it now"; it's running it in name only —
summarizing your own intent instead of reading the actual files, or marking
it passed without having genuinely worked the checklist. If a genuine run
(now or earlier) finds something, stop here and report it.

## Gate — check before doing anything else

1. Has `blackink-review` been run against the CURRENT state of the diff
   (not an earlier, since-edited version of it) in this conversation?
   - **No** → run it now (`/blackink-review` or invoke the skill directly).
     If it surfaces findings, stop and report them — fix or get explicit
     user sign-off on deferring each one before continuing this skill.
   - **Yes, and it passed clean** → continue.
   - **Yes, but it found something** → confirm each finding is either fixed
     or explicitly accepted by the user (a one-line "yes, ship it anyway"
     is enough — don't require a essay, but don't proceed on silence).
2. Confirm Step 0 of that review (the full test suite) was actually run
   against the code as it stands right now, not before the last edit. If
   any file changed since that test run, re-run the suite before
   proceeding — a passed review against stale code is not a passed review.

Only after both checks hold do you proceed to branching and opening the PR.

## Branching

- If already on a dedicated task/feature branch with only this task's
  commits, you may stay on it — don't create a redundant branch.
- Otherwise create one named for the task
  (`week{N}-subtask-{X.Y.Z}-{short-slug}`, matching this repo's existing
  branch-naming convention — check `git branch -a` for the pattern already
  in use before inventing a new one).
- Per this repo's own standing rule: never merge commits between stacked
  branches — cherry-pick or rebase. Never force-push, never
  `git reset --hard`, never skip hooks, unless the user has explicitly asked
  for that specific action in this conversation.
- Confirm with the user before pushing — pushing and opening a PR are
  shared-state actions per this session's own risk policy, and a prior
  approval for one push does not cover this one.

## PR title

This repo has two real, already-in-use title conventions (checked via
`gh pr list` — don't invent a third):

- **Numbered subtask work**: `Subtask {X.Y.Z}: {Title Case Description}`
  (e.g. "Subtask 1.2.2: 50/50 Settlement Split Engine & 60-Day Clawback
  Monitor"). For a PR spanning several subtasks in one week, use
  `Week {N} {optional Dev label}: {Description} ({X.Y.Z, X.Y.Z, ...})` (e.g.
  "Week 2 Dev 1: Appointment Ops, Payment Auth & Settlement Engine (1.1.1,
  1.2.1, 1.2.2)").
- **Unnumbered feature/fix work** (no client-spec subtask number attached):
  Conventional-commit style, `type(scope): description` — `feat(...)`,
  `fix(...)` — lowercase, imperative, matching this repo's own commit-message
  style (e.g. "feat(ui-api): internal read endpoints for Blackink UI
  dashboard", "fix: exclude demo sandbox client from daily digest metrics").

Pick whichever family matches the work; don't mix them in one title (a
subtask number AND a `feat(...)` prefix on the same PR). If genuinely
unsure which family fits, ask rather than guessing.

## PR description — required structure

The PR body MUST have exactly these three sections, in this order. Don't
skip "Remaining" just because nothing is left — say so explicitly ("Nothing
outstanding — all DoD lines verified, see Testing below").

```markdown
## Done

- What actually shipped, in concrete terms (not "implemented the feature" —
  name the tables/files/rules). Pull this from the task's own DoD/spec, not
  from memory of what was intended.
- Verification performed: which tests (pure + live-DB), which manual/
  real-service checks (e.g. real Stripe test mode), and their actual result
  — not "should work."

## Remaining

- Anything asked for that is deliberately deferred, stubbed, or out of
  scope for this PR — traced to a reason (a dependency that doesn't exist
  yet, a client question still open, a decision the user made to descope).
  Never silently omit a gap here that blackink-review's §1 (scope
  completeness) surfaced.
- Any open client/product question the work proceeded under an assumption
  for (per this repo's standing rule that a deviation needs a traced
  reason, not a silent gap) — list the assumption made and what confirming
  otherwise would change.

## How to finish

- For each "Remaining" item: the concrete next step (which file/module to
  touch, what decision is needed first, what it's blocked on). Enough for
  someone else — or future-you — to pick it up without re-deriving the
  context in this conversation.
- If nothing is remaining, this section states that plainly instead of
  being omitted.
```

Append the standard `🤖 Generated with Claude Code` footer per this
session's own PR-creation convention; don't duplicate content already in
commit messages verbatim — summarize, don't paste the whole diff.

## After opening

Return the PR URL to the user. Do not merge it, do not request reviewers
add themselves, do not close linked issues — this skill's job ends at
"PR opened," same boundary as this session's standing PR-creation
instructions.
