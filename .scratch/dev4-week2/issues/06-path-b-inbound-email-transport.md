# Research: Path B inbound email transport

Type: research
Status: resolved
Blocked by: —

## Question

Path B ingests leads arriving at `leads@{client-subdomain}.getblackink.com` — the app must
receive inbound email to parse it. Repo has outbound SMTP only; no inbound receiving.

Research and recommend the transport: inbound-parse webhook (e.g. Mailgun Routes, SendGrid
Inbound Parse, Postmark Inbound) vs IMAP polling. Capture for each: how mail reaches the app
(push vs poll), latency (Path B DoD = process within 5s of receipt), DNS/MX requirements,
per-client subdomain address support (`leads@{sub}.getblackink.com`), signature/auth
verification, cost, and fit with the existing manual-DNS deliverability runbook. Recommend
one with rationale. Findings on a throwaway `research/path-b-inbound-transport` branch;
link from this ticket.

## Answer

**Mailgun Routes (forward-to-webhook).** Full findings: `docs/research/path-b-inbound-transport.md`
on branch `research/path-b-inbound-transport` (commit `b30eb2e`).

- **Push, meets 5s DoD** — Mailgun POSTs the parsed message on receipt; no poll floor.
- **Per-client subdomain = one manual DNS record** — a single wildcard MX
  `*.getblackink.com` → Mailgun + one regex `match_recipient` catch-all route serves every
  `leads@{sub}.getblackink.com`; app parses recipient for `client_id`. New clients need zero
  new DNS/provider config → best fit for the hands-off manual-DNS runbook.
- **HMAC-SHA256 signed webhooks** — verify signature BEFORE any DB write (tenant-safety
  boundary), then write `inbound_messages` under `session_scope(client_id=...)`.
- **No per-inbound-message charge** — gated by route count, not volume.

Rejected: SendGrid Inbound Parse (catch-all host-scoped → recurring per-tenant config);
Postmark (no HMAC, inbound consumes send quota); IMAP poll (can't reliably hit 5s, per-seat cost).
