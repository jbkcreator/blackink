# Design the config-driven pay-per-lead parser registry

Type: grilling
Status: open
Blocked by: 07, 11

## Question

Path B must recognise APM / Manage My Property / Thumbtack notification emails and tag
`source_channel` accordingly, entering the same Respond queue + 30-min SLA + non-poach gate.
Requirement: config-driven — adding a 4th portal needs only a new config row + parser
function, no orchestrator change.

Decide: the config-row shape (sender address + subject pattern per portal), the parser
registry/dispatch mechanism (how a message routes to its parser), the `source_channel`
enum values (align with ticket 5), and the fall-through to an `UNCLASSIFIED` bucket with
`requires_human_review = TRUE` (no crash, no silent drop). Use the ticket-11 samples to
confirm the config fields are sufficient. Output: the registry contract + config schema.
