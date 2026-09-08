# Collect APM / MMP / Thumbtack sample notification emails

Type: task
Status: open
Blocked by: —

## Question

Pay-per-lead parsing (4.2.3) needs the real notification-email formats from All Property
Management (APM), Manage My Property, and Thumbtack to write reliable extractors — the
parser design (ticket 12) is blocked until we have concrete samples.

HITL task: obtain at least one real (or vendor-documented) sample notification email per
portal, capturing sender address, subject-line pattern, and body layout for the fields we
must extract (owner name, property address, phone, inquiry text; Thumbtack also job
description + budget). Dev 4 to source these from the client's existing portal accounts or
the vendors. Answer records where the samples are stored (path/attachment) and the
per-portal sender + subject patterns.
