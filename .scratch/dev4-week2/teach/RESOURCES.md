# Resources

## Primary (high-trust, in-repo)

- **Implementation Blueprint v2** — `docs/Implementation_Blueprint_v2.md`. The source of truth
  for the sending-pool architecture (§ lines ~640–695), Path A/B Speed-to-Lead flow (~663–684),
  onboarding State 12/13 sending-identity + inbound config (~775–776), and the Path B cloner
  runbook (~819–865).
- **Week 2 task split** — `docs/Week2_Tasks_Dev_Split_v1.md` (Dev 4 = Tasks 4.1/4.2/4.3).
- **Sept 04 client triage** — `docs/Sept04_New_Items_Triage.md`.
- **Path B transport research** — `docs/research/path-b-inbound-transport.md` (Mailgun recommendation).

## Code (ground truth)

- `src/services/mailbox_dispatcher.py` — mailbox picker / rotation / cap.
- `src/tasks/deliverability_sentinel.py` — quarantine + reserve swap.
- `migrations/apply_sending_domains.py` — domains + mailboxes schema.
