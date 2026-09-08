"""Approved outbound copy for the 3-touch win-back sequence (Subtask 3.1.2).

Same convention as sequence_content.py and this codebase's other
personalized artifacts (the LinkedIn deep-link note, the Fee-Stack
one-pager, the Owner Visibility Score PDF): a fixed, Blackink-authored,
merge-tag-populated template — not a freeform LLM generator. A human still
gates every send via the #blackink-setter Slack approval card, so nothing
reaches an owner unreviewed even though the first draft is written here.

render_winback_touch() returns (subject, body, template_version).
audit_loss_dollars_est has no computation anywhere in this codebase yet
(see the 3.1.2 plan doc) — Touch 1 falls back to a generic market-shift
hook when it's None, never a fabricated number. booking_url is likewise
optional — Touch 3 falls back to a plain "just reply" close when no
client-owned booking link has been provisioned yet
(src/services/booking_link.py::resolve_owner_booking_link).
"""

from __future__ import annotations

from typing import Optional

WINBACK_TOUCH_STEPS = (1, 2, 3)

_TEMPLATE_VERSION = "v1"


def _greeting(owner_name: Optional[str]) -> str:
	first_name = (owner_name or "").split()[0] if owner_name else None
	return f"Hi {first_name}," if first_name else "Hi there,"


def render_winback_touch(
	touch_step: int,
	*,
	owner_name: str,
	county_name: str,
	property_address: str,
	audit_loss_dollars_est: Optional[int] = None,
	booking_url: Optional[str] = None,
) -> tuple[str, str, str]:
	"""Return (subject, body, template_version) for a win-back touch.

	Raises ValueError for a step outside WINBACK_TOUCH_STEPS."""
	if touch_step not in WINBACK_TOUCH_STEPS:
		raise ValueError(f"touch_step {touch_step} is not a win-back touch (expected one of {WINBACK_TOUCH_STEPS})")

	greeting = _greeting(owner_name)
	template_version = f"wb{touch_step}_{_TEMPLATE_VERSION}"

	if touch_step == 1:
		subject = f"An update on {property_address}"
		if audit_loss_dollars_est:
			hook = (
				f"Owners like you in {county_name} are leaving an estimated "
				f"${audit_loss_dollars_est:,}/yr on the table self-managing — "
				"mostly from fee lines and pricing that a managed portfolio "
				"captures automatically."
			)
		else:
			hook = (
				f"The rental market in {county_name} has shifted enough since we "
				f"last worked together that it's worth another look at {property_address}."
			)
		body = (
			f"{greeting}\n\n{hook}\n\n"
			"Worth 15 minutes to see what's changed and whether it's worth "
			"picking management back up?\n\n"
			"Best,\nThe Blackink team"
		)
	elif touch_step == 2:
		subject = f"Re: An update on {property_address}"
		body = (
			f"{greeting}\n\n"
			"Following up — most self-managing owners we talk to aren't capturing "
			"every fee line they're entitled to: lease renewal fees, maintenance "
			"markups, tenant setup fees, and pet rent share are the most common "
			"ones left on the table.\n\n"
			f"Happy to walk through what that looks like for {property_address} "
			"specifically. Want me to send it over?\n\n"
			"Best,\nThe Blackink team"
		)
	else:  # touch_step == 3
		subject = f"Re: An update on {property_address}"
		closing = (
			f"Grab 15 minutes here: {booking_url}"
			if booking_url
			else "Just reply and I'll find a time that works."
		)
		body = (
			f"{greeting}\n\n"
			"I'll close the loop here — if picking management back up is worth "
			f"exploring for {property_address}, happy to talk it through, no "
			f"obligation. {closing}\n\n"
			"Best,\nThe Blackink team"
		)

	return subject, body, template_version
