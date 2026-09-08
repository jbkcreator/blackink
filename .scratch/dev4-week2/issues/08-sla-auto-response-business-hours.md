# Design the 30-min SLA auto-response & business hours

Type: grilling
Status: resolved
Blocked by: 05

## Answer

Locked via grilling 2026-09-07 — see `.scratch/dev4-week2/SPEC-4.2.1.md` §3. Business hours =
fixed ET (08:00–18:00 Mon–Fri), config seam for per-client later. `send_at` = now if in-hours
else next business-open; a sweep dispatches due responses. `sla_due_at = received_at + 30 min`.
Content = static per-client template + merge fields + `booking_link.py`, no LLM. Email only.
Closer Slack card fires immediately regardless of deferral.

## Question

A lead must get a personalised auto-response with a booking link within 30 min during
business hours (next business morning after hours), plus a closer alert card to
`#blackink-setter`, and `sla_due_at = received_at + 30 min`.

Decide: business-hours definition and timezone source (repo has
`apply_area_code_timezones.py` — derive tz from prospect area code, or per-client config?);
after-hours deferral mechanism (queue to next business morning); auto-response content
template + reuse of `booking_link.py`; the closer-alert card format (reuse triage/context
card shape). Email only — no SMS. Output: the SLA timer + deferral + response-content spec.
