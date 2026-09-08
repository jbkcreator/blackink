# Cadence engine: homegrown scheduler vs GHL

Type: grilling
Status: open
Blocked by: —

## Question

The six-attempt cadence spec says "via GHL sequence" with a per-client GHL sequence ID in a
config row. But the repo already has a homegrown sequence scheduler
(`sequence_dispatcher.py` / `sequence_orchestrator.py` / `sequence_enrollment.py`).

Decide: build the 5-further-attempt inbound cadence on the **homegrown** scheduler, or
integrate the **GHL API**? Key fact needed from the client/team (Dev 4 to relay): is GHL
already contracted and are API credentials + a workspace available? If not, homegrown avoids
a new dependency and reuses proven scheduling. If GHL is contracted, define the config-row
schema and API arming path. This choice gates the cadence design (ticket 10).

**Parked**: awaits the client/team answer on GHL contracting. Do not self-answer.
