"""Group D / D-6 — the real residual gap, distinct from the workflow's
now-deleted path filter.

The audit's original D-6 claimed a CI path filter meant "a PR adding a new
SMS send path elsewhere would not run the gate." That was false —
.github/workflows/tests.yml already runs the full compliance test suite
unfiltered on every PR — so compliance_gates_ci.yml was pure duplication
and has been deleted.

The gap that filter never addressed, and neither workflow does: nothing
asserts every outbound send path actually CALLS a compliance gate before
dispatch. A brand-new send path that simply never imports the gate passes
every existing test green. Same structural-test technique as
tests/test_billing_structural.py and tests/test_no_upfront_charge_paths.py.

Scope, stated explicitly: this covers the two established cold-outbound
campaign systems that share compliance_gate.py's per-contact eligibility
concept — src/services/sequence_orchestrator.py (evaluate_touch_gate) and
src/services/winback_sequencer.py (its own parallel gate, documented in
that file as deliberately not reusing compliance_gate.py's table shape,
but built on the same PASS/GateCheckResult primitives).

Deliberately NOT covered: src/services/stl_cadence.py. It dispatches
follow-ups to an inbound lead who already contacted the client — an
opted-in reply-response flow, not cold outbound — and checks its own
cadence-stop latch (stl_cadence_touch_still_ready) rather than
compliance_gate.py's DNC/non-poach/quiet-hours waterfall. That is a
design question for whoever owns Speed-to-Lead compliance scope, not a
Group D defect to silently paper over here.
"""
from pathlib import Path

SRC_SERVICES = Path(__file__).resolve().parent.parent / "src" / "services"

_GATED_SEND_MODULES = {
    "sequence_orchestrator.py": "evaluate_touch_gate",
    "winback_sequencer.py": "GateCheckResult",
}


def test_known_cold_outbound_modules_import_a_compliance_gate():
    for filename, gate_symbol in _GATED_SEND_MODULES.items():
        path = SRC_SERVICES / filename
        assert path.exists(), f"{filename} not found — has it moved? Update this test's scope."
        text = path.read_text(encoding="utf-8", errors="replace")
        assert gate_symbol in text, (
            f"{filename} no longer references {gate_symbol} — a cold-outbound "
            f"send path must not lose its compliance gate silently."
        )


def test_email_sender_callers_are_the_known_reviewed_set():
    """Every module that can actually place an SMTP send (build_email_sender)
    must be one this test knows about. A NEW caller appearing here is exactly
    the "new send path bypassing the gate" scenario D-6 named — it must force
    a conscious addition to this test (with a gating decision), not merge
    silently."""
    known_callers = {
        "email_sender.py",          # the primitive itself
        "sequence_orchestrator.py",  # cold outbound — gated, see above
        "winback_sequencer.py",      # win-back outbound — gated, see above
        "stl_cadence.py",            # STL follow-ups — see module docstring above
        "speed_to_lead_sweep.py",    # STL initial auto-response — inbound-reply, not cold outbound
        # S-11 — Slack "Reply in Thread" / "Book Meeting" button handlers
        # (src/services/slack/listeners.py). Both load an existing
        # inbound_messages row the contact themselves sent in
        # (_load_inbound_message) — an opted-in inbound reply, same category
        # as speed_to_lead_sweep.py above, not a cold-outbound campaign
        # contact compliance_gate.py's DNC/non-poach/quiet-hours waterfall is
        # meant to screen. Gated instead by what an inbound reply actually
        # needs: _is_opted_out() before sending, approver_authorized() so an
        # arbitrary channel member can't trigger a real send, and an atomic
        # claim (_claim_inbound_message_for_send) so a double-click/replay
        # can't double-send.
        "listeners.py",
    }
    offenders = []
    for path in SRC_SERVICES.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        if path.name in known_callers:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "build_email_sender" in text:
            offenders.append(str(path.relative_to(SRC_SERVICES.parent.parent)))

    tasks_root = SRC_SERVICES.parent / "tasks"
    for path in tasks_root.rglob("*.py"):
        if "__pycache__" in path.parts or path.name in known_callers:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "build_email_sender" in text:
            offenders.append(str(path.relative_to(SRC_SERVICES.parent.parent)))

    assert offenders == [], (
        "new module(s) call build_email_sender() that this test doesn't know "
        f"about — decide and encode their compliance-gating before adding "
        f"them to known_callers: {offenders}"
    )
