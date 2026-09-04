# TASK: provision the Sendspark workspace and record the base video

Label: `wayfinder:task`
Status: closed — OUT OF SCOPE
Assignee: —
Blocked by: —

## ⛔ Out of scope (2026-09-03) — do not action

**Do not purchase a Sendspark plan.** The Source of Truth cancels this for
September:

> "September includes a video trigger hook and **disabled/configured provider
> row only**, **not the video generation/delivery flow**. **No Sendspark
> account was supplied.**" — §1.5
>
> "If video is later hosted, use `watch.getblackink.com`." — §1.5
>
> "Replace ghost-shopper claims, **live video**, metro ranks, and active seat
> scarcity with permitted public evidence and actual enabled offers." — §2.3

This ticket was opened on the strength of ticket 10's research, which
concluded Sendspark was technically fit for purpose. That finding stands
technically and is wrong commercially — the client is not doing video in
September.

**What September actually needs instead** — much smaller, and folded into
ticket 19:

- a **video trigger hook** (the call site, wired but inert)
- a **disabled/configured provider row** for the video provider
- nothing else: no account, no credentials, no base video, no GIF, no
  per-prospect enrollment

Do not add `SENDSPARK_API_KEY` / `_SECRET` / `_WORKSPACE_ID` to settings for a
provider that is disabled. If a provider row needs a config shape, keep it
generic rather than Sendspark-specific — the vendor is not committed.

Reopen only if the client reverses the video hold.

## Question

Not a decision — manual work that blocks several decisions. Ticket 10
established Sendspark is fit for purpose; nothing exists yet to use it with.

**HITL.** Most of this needs a human with billing authority and a webcam.

Checklist:

1. **Provision a workspace on Growth or above** ($99/mo, 250 personalized
   minutes). API access and webhooks are gated below Growth, so Solo will
   not work. Confirm who owns the billing relationship.
2. **Mint credentials** in the API Credentials tab of workspace settings:
   `x-api-key` (workspace-scoped) and `x-api-secret` (user-scoped). Note
   the user-scoping — if it is tied to an individual's profile, decide
   whether that should be a shared service account rather than a person who
   might leave.
3. **Store them** as `SENDSPARK_API_KEY`, `SENDSPARK_API_SECRET`,
   `SENDSPARK_WORKSPACE_ID` in `config/settings.py` via pydantic-settings,
   reachable through `get_settings()` — never `os.environ` directly
   (`CLAUDE.md`). Add to `.env.example`. Do **not** commit real values.
4. **Record the base video** for the Touch 1 campaign. One recording, reused
   for every prospect. Needs a script — coordinate with whoever owns
   outbound copy, since it must work generically across every prospect while
   the personalization comes from the website overlay.
5. **Create the dynamic campaign** via
   `POST /workspaces/{workspaceId}/dynamics` and record the returned
   `dynamicId` — the sequencer needs it to enroll prospects.
6. **Verify end to end** with one real prospect POST: confirm `videoLink`,
   `embedLink`, and `thumbnailUrl` come back, and that the GIF actually
   renders the prospect's website.
7. **Register the webhook endpoint** — though the receiver does not exist
   yet, so this may need to wait on 3.1.3's ingestion work.

## Answer should record

Workspace id, `dynamicId`, where credentials live, what the base video says,
and the actual observed per-prospect latency from POST to `thumbnailUrl`
being fetchable — later tickets depend on all of these.

Note `processAndAuthorizeCharge: true` is required on the prospect call or
it is rejected at the plan limit. Decide whether Blackink always sets it,
and what the overage exposure is at expected volume.
