# Provision sending domains & DNS (manual runbook)

Type: task
Status: open
Blocked by: —

## Question

Task 4.1 needs 5 internal domains (`growth-/connect-/audit-/pm-/scale-getblackink.com`,
2 mailboxes each) + 2 founding-client cluster domains provisioned, with SPF/DKIM/DMARC
verified green (MXToolbox or equivalent). Per CLAUDE.md / Forced Action ADR 0011,
DNS + mailbox warmup is a **manual runbook, not code** — `sending_domains`/`mailboxes`
only track state.

This is a HITL task: produce the precise provisioning checklist (registrar/DNS steps,
records to add, mailbox creation, warmup start), execute it (or hand it to whoever holds
registrar access), and record the resulting facts later tickets need: domain names,
cluster labels, mailbox addresses, SMTP credential location, warmup start dates, and DNS
verification screenshots posted to `#blackink-qa`.
