"""Approved outbound copy for the 5-touch sequence.

Review-finding follow-up: dispatch_touch is fail-closed on missing content
(returns NO_CONTENT and never sends), so *something* must persist a real,
approved subject/body into each email touch's work-order payload at enrollment
time. Until the upstream LLM copy generator lands (Dev 2, ticket 19), these
versioned templates ARE the approved copy — a human still gates every send via
the Slack approval card, so nothing goes out un-reviewed.

render_touch() returns (subject, body, template_version). template_version
names the copy variant so a later A/B or LLM swap is traceable in
sequence_touch_dispatches. Only email touches (1, 3, 5) have copy here; phone
and LinkedIn touches are performed manually and carry no email body.
"""

from __future__ import annotations

# Only email touch steps have renderable copy. Keep in sync with
# sequence_enrollment._EMAIL_TOUCH_STEPS.
EMAIL_TOUCH_STEPS = (1, 3, 5)

_TEMPLATE_VERSION = "v1"


def _greeting(first_name: str | None) -> str:
    return f"Hi {first_name}," if first_name else "Hi there,"


def _company_phrase(company_name: str | None) -> str:
    return f"at {company_name}" if company_name else "at your firm"


def render_touch(
    touch_step: int,
    *,
    first_name: str | None = None,
    company_name: str | None = None,
) -> tuple[str, str, str]:
    """Return (subject, body, template_version) for an email touch.

    Raises ValueError for a non-email touch step — callers should only render
    copy for steps in EMAIL_TOUCH_STEPS.
    """
    if touch_step not in EMAIL_TOUCH_STEPS:
        raise ValueError(f"touch_step {touch_step} is not an email touch (expected one of {EMAIL_TOUCH_STEPS})")

    greeting = _greeting(first_name)
    company = _company_phrase(company_name)
    template_version = f"t{touch_step}_{_TEMPLATE_VERSION}"

    if touch_step == 1:
        subject = "Owner visibility for the doors you manage"
        body = (
            f"{greeting}\n\n"
            f"We help property-management teams {company} surface which of their "
            "owners hold additional doors elsewhere — the ones most likely to hand "
            "you more units if you ask first.\n\n"
            "Worth a short look at what that would show for your portfolio?\n\n"
            "Best,\nThe Blackink team"
        )
    elif touch_step == 3:
        # Threads as a reply to touch 1 (In-Reply-To wired by the orchestrator).
        subject = "Re: Owner visibility for the doors you manage"
        body = (
            f"{greeting}\n\n"
            "Following up on my note — most teams we work with are surprised how "
            "many of their current owners quietly hold doors with a competitor.\n\n"
            "Happy to send a sample owner-visibility snapshot for your market. Want me to?\n\n"
            "Best,\nThe Blackink team"
        )
    else:  # touch_step == 5
        subject = "Re: Owner visibility for the doors you manage"
        body = (
            f"{greeting}\n\n"
            "I'll close the loop here. If growing managed doors from your existing "
            "owner base is on the roadmap this quarter, we can show exactly where "
            "the opportunity sits — no obligation.\n\n"
            "Just reply and I'll set it up.\n\n"
            "Best,\nThe Blackink team"
        )

    return subject, body, template_version
