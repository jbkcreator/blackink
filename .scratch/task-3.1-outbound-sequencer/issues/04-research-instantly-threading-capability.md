# RESEARCH: can Instantly's API deliver per-message threading and tracking?

Label: `wayfinder:research`
Status: closed
Assignee: research subagent
Blocked by: —

## Resolution

**Instantly cannot be the sender for Task 3.1.** Three independent
disqualifiers, any one of which is fatal:

1. **`In-Reply-To` cannot be set.** Threading is expressed only as
   `reply_to_uuid`, Instantly's internal row id; it synthesises RFC headers
   itself. Touch 3's DoD (§5.2) is unmeetable.
2. **No attachments.** Instantly's help centre directs users to link cloud
   storage instead. Touch 1's PDF (§4.1, §4.4) is impossible.
3. **No general send endpoint.** The email API group is `test` / `reply` /
   `forward` plus reads. Touch 1 could only originate from a campaign
   Instantly paces itself — so mailbox selection and the 30–50/day cap
   (§7.1, §7.2) leave our control.

`message_id` *is* readable after send, but only post-hoc, not at compose
time — which does not help, since the constraint is setting the header on
Touch 3, not reading it from Touch 1.

**Tracking is the one salvageable piece:** `open_tracking` / `link_tracking`
with webhook events `email_opened` / `email_link_clicked`, mapping cleanly
onto the names §27.1 mandates. Plan-gated (Hypergrowth, ~$97/mo).

**Hands ticket 05 its deciding axis: who mints the `Message-ID`.** Direct
SMTP is the only option giving outright header control plus a literal "send
from these 6 warmed mailboxes", at the cost of self-hosting open/click
tracking. Postmark / SES / Resend / SendGrid all give header control,
attachments, and tracking webhooks — note SES overwrites `Message-ID` with
its own (workable: read it back), and Postmark's custom `Message-ID`
preservation reportedly needs `X-PM-KeepID: true`, which appears in support
guidance but not the API reference and is **unverified**.

Full findings with citations below.

## Question

Touch 3's Definition of Done (§5.2) hard-requires that its `In-Reply-To`
header equals Touch 1's `Message-ID`, and that the subject carries a `Re:`
relationship. That is a per-message header requirement.

`InstantlyService` in this repo is analytics-only — `get_warmup_analytics`,
`get_daily_analytics`, `list_accounts` — and its own docstring defers
campaign CRUD to "whenever the actual Campaign Agent dispatch workstream
lands". `outbound_templates.build_instantly_sequence` suggests Instantly was
the intended sender. But Instantly is a *campaign sequencer*: it may own
threading itself and not expose it.

Establish, from Instantly's actual API documentation:

1. Does the API allow sending a single, individually-addressed email — as
   opposed to enrolling a lead into a multi-step campaign it then paces
   itself?
2. Is the resulting **`Message-ID` returned or readable** via API or webhook?
3. Can `In-Reply-To` / `References` headers be **set** on an outbound
   message? Or does Instantly thread its own follow-up steps internally with
   no caller control?
4. **Attachments** — can a per-recipient PDF be attached? Size limits?
5. **Open and click tracking** — available, and exposed via webhook? What
   are the webhook event names and payload shapes? Can they be mapped onto
   `email_opened` / `email_clicked`?
6. **Per-mailbox send caps** — does Instantly enforce its own pacing, and if
   so does that cooperate or conflict with our 30–50/mailbox/day requirement
   (§7.2)?
7. What does the API cost / rate-limit look like at 6 mailboxes per client?

If Instantly cannot set `In-Reply-To`, say so plainly and loudly — that
single fact eliminates it as the sender for Touch 3 and decides ticket 05.

Also survey, briefly, what the realistic alternative looks like: direct SMTP
per warmed mailbox, or a transactional provider (Postmark, SES, Resend) —
specifically whether each gives full header control plus open/click tracking,
since that is the combination Task 3.1.1 needs.

## Output

Findings on a throwaway `research/instantly-threading` branch, or as a
comment on this ticket if short. Cite documentation URLs — do not infer
capability from the vendor's marketing pages.

## Research findings

Researched 2026-09-03 against Instantly's live API reference
(https://developer.instantly.ai). **Verdict: Instantly cannot be the sender
for this sequence.** Detail below, question by question.

### 1. No general "send an email" endpoint

The Email group exposes only: `POST /api/v2/emails/test`, `POST
/api/v2/emails/reply`, `POST /api/v2/emails/forward`, `GET /api/v2/emails`,
`GET /api/v2/emails/{id}`, `PATCH`, `DELETE`, `GET
/api/v2/emails/unread/count`, `POST /api/v2/emails/threads/{id}/mark-as-read`
(https://developer.instantly.ai/api-reference/groups/email). There is **no
`POST /api/v2/emails` send**. An initial cold email can only originate from a
campaign that Instantly itself paces. Touch 1 therefore cannot be a
code-initiated single send.

### 2. `Message-ID` is readable — partially

The reply endpoint's response (an Email object) does include `message_id`
("unique email ID from the email server") and `thread_id`
(https://developer.instantly.ai/api-reference/email/reply-to-an-email). So
read-back exists for emails Instantly has recorded. But it is only reachable
after the campaign has sent; we cannot obtain it at compose time.

### 3. `In-Reply-To` / `References` cannot be set — **hard blocker**

The reply endpoint's body is exactly: `eaccount`, `reply_to_uuid`, `subject`,
`body{html,text}`, `additional_recipients`, `cc_address_email_list`,
`bcc_address_email_list`, `reminder_ts`, `assigned_to`. No headers object, no
`In-Reply-To`, no `References`, no `Message-ID`
(https://developer.instantly.ai/api-reference/email/reply-to-an-email).
Threading is expressed only as `reply_to_uuid` — Instantly's own row id — and
Instantly synthesises the headers internally. Campaign creation is the same
story: steps/variants carry only `subject` and `body`, with no header field
(https://developer.instantly.ai/api-reference/campaign/create-campaign).

In-campaign threading is a UI convention, not an API contract: Instantly
threads a follow-up when the subject is left blank and the *same sending
account* is still in the campaign; it explicitly does not document choosing
which mailbox sends a given step
(https://help.instantly.ai/en/articles/7914807-email-threading). That
conflicts directly with "our code selects the mailbox".

**So: we cannot satisfy "Touch 3's `In-Reply-To` == Touch 1's `Message-ID`"
via Instantly's API.** Ticket 05 should treat Instantly as eliminated for
sending. `build_instantly_sequence` and any dispatch plan built on
`InstantlyService` are dead ends; keep `InstantlyService` for warmup/deliverability
analytics only.

### 4. Attachments — not supported

Campaign step variants have no attachment field
(https://developer.instantly.ai/api-reference/campaign/create-campaign), and
Instantly's own help centre tells users to link to cloud storage instead of
attaching files. A per-recipient PDF is not possible. (A search snippet
claimed a hosted-URL `attachments` field on a `POST /api/v2/emails`; that
endpoint does not exist in the reference — treat the claim as unverified.)

### 5. Tracking / webhooks — this part does work

`open_tracking` (required bool) and `link_tracking` on campaign create.
Webhook `event_type` enum includes `email_sent`, `email_opened`,
`email_link_clicked`, `reply_received`, `email_bounced`,
`lead_unsubscribed`, `campaign_completed`, `account_error`, plus lead-status
events (https://developer.instantly.ai/api-reference/webhook/create-webhook).
`email_opened` → our `email_opened`; `email_link_clicked` → `email_clicked`.
Payload carries timestamp, event_type, campaign_id/name, workspace. Webhooks
are a Hypergrowth-plan ($97/mo) feature per Instantly pricing — verify before
budgeting.

### 6. Pacing — Instantly owns it

`daily_limit` and `email_gap` (minutes) are campaign-level, and Instantly
paces sends itself. Our 30–50/mailbox/day and our own per-contact scheduler
would be fighting it, not cooperating with it.

### 7. Rate limits / cost

Not stated numerically in the public API reference; plan-gated features
(webhooks) are the practical constraint. Not worth pinning down given 3.

### Alternatives

| | Set `In-Reply-To`/`References` | Read back `Message-ID` | Per-recipient attachment | Open/click webhooks | Send as arbitrary mailbox we own |
|---|---|---|---|---|---|
| **Direct SMTP per mailbox** | Yes — we build the MIME, we mint the `Message-ID` | Yes — we chose it | Yes | **No** — must self-host pixel + link redirect | Yes, that *is* the mailbox |
| **Postmark** | Yes, `Headers[]` array | Yes, but its `MessageID` is Postmark's own; preserving a custom RFC `Message-ID` reportedly needs `X-PM-KeepID: true` (support guidance, not in the API ref — verify) | Yes, base64 `Attachments[]`, 50 MB payload cap on batch | Yes (Open/Click webhooks) | Yes, any verified sender signature/domain |
| **Amazon SES v2** | Yes (raw MIME, or custom headers on SendEmail) | Returns SES `MessageId`; SES **overwrites** your `Message-ID` header with `<id@region.amazonses.com>` — so mint from the returned id | Yes, via raw MIME | Yes, config-set event destinations (Open, Click) via SNS/EventBridge | Yes, verified domain/identity |
| **Resend** | Yes, custom `headers` (and native `reply_to` threading) | Yes, returns `id` | Yes | Yes | Yes, verified domain |
| **SendGrid** | Yes, `headers` per personalization | Returns `X-Message-Id`, not the RFC `Message-ID` | Yes | Yes (Event Webhook) | Yes, authenticated domain |

Docs: https://postmarkapp.com/developer/api/email-api ·
https://docs.aws.amazon.com/ses/latest/dg/send-email-raw.html ·
https://docs.aws.amazon.com/ses/latest/dg/event-publishing-send-email.html ·
https://resend.com/docs/dashboard/emails/custom-headers ·
https://www.twilio.com/docs/sendgrid/for-developers/sending-email/personalizations

Recommendation for ticket 05: the deciding axis is *who mints the
`Message-ID`*. Direct SMTP is the only option where we control it outright
and where "send from these 6 specific warmed mailboxes" is literal rather
than an approximation — at the cost of self-hosting open/click tracking.
Postmark/SES/Resend give tracking for free but send from a provider-verified
domain, which is a different deliverability posture than warmed inboxes, and
SES specifically rewrites `Message-ID` (workable: read it back from the API
response, store it, use it as Touch 3's `In-Reply-To`).
