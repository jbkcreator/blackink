"""Unit tests for Task 4.2.3 portal parsers — no DB required."""

import pytest

from src.services.portal_parsers import (
    UNCLASSIFIED,
    EmailParts,
    ParsedLead,
    classify_and_parse,
    parse_apm,
    parse_manage_my_property,
    parse_thumbtack,
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _parts(sender="", subject="", body="", html=None) -> EmailParts:
    return EmailParts(
        sender=sender,
        subject=subject,
        body_plain=body,
        body_html=html,
    )


APM_BODY = """\
Owner Name: Jane Smith
Email: jane@example.com
Phone: 555-123-4567
Property Address: 123 Oak St, Denver, CO 80201
Message: I own several units and need a property manager.
"""

MMP_BODY = """\
Owner: John Doe
E-mail: john.doe@example.com
Best Contact Number: (720) 555-9876
Property Location: 456 Maple Ave, Aurora, CO 80010
Inquiry: Looking for full-service management for 3 units.
"""

THUMBTACK_BODY = """\
Name: Alice Johnson
Email: alice@example.com
Phone: 303-555-0011
Location: Denver, CO
Job: Property management for 2-unit residential
Budget: $150/month
"""


# ── APM parser ────────────────────────────────────────────────────────────────

class TestParseAPM:
    def test_extracts_all_fields(self):
        result = parse_apm(_parts(body=APM_BODY))
        assert result.source_channel == "APM"
        assert result.prospect_name == "Jane Smith"
        assert result.email == "jane@example.com"
        assert result.phone is not None and "555" in result.phone
        assert result.property_address is not None and "Oak St" in result.property_address
        assert result.requires_human_review is False

    def test_partial_body_degrades_gracefully(self):
        result = parse_apm(_parts(body="Email: badactor@evil.com\nSome body text"))
        # name is missing → review flagged
        assert result.requires_human_review is True
        assert result.source_channel == "APM"
        assert result.email == "badactor@evil.com"

    def test_body_wide_email_fallback(self):
        body = "No labels here. Contact us at noname@example.com for details."
        result = parse_apm(_parts(body=body))
        assert result.email == "noname@example.com"


# ── Manage My Property parser ─────────────────────────────────────────────────

class TestParseManageMyProperty:
    def test_extracts_all_fields(self):
        result = parse_manage_my_property(_parts(body=MMP_BODY))
        assert result.source_channel == "MANAGE_MY_PROPERTY"
        assert result.prospect_name == "John Doe"
        assert result.email == "john.doe@example.com"
        assert result.phone is not None and "720" in result.phone
        assert result.property_address is not None and "Maple" in result.property_address
        assert result.requires_human_review is False

    def test_missing_phone_still_usable(self):
        body = "Owner: Bob\nEmail: bob@example.com\nProperty: 789 Pine Rd"
        result = parse_manage_my_property(_parts(body=body))
        assert result.prospect_name == "Bob"
        assert result.email == "bob@example.com"
        assert result.requires_human_review is False  # name + email present


# ── Thumbtack parser ──────────────────────────────────────────────────────────

class TestParseThumbTack:
    def test_extracts_all_fields(self):
        result = parse_thumbtack(_parts(body=THUMBTACK_BODY))
        assert result.source_channel == "THUMBTACK"
        assert result.prospect_name == "Alice Johnson"
        assert result.email == "alice@example.com"
        assert result.phone is not None and "303" in result.phone
        assert result.property_address is not None and "Denver" in result.property_address
        assert result.requires_human_review is False

    def test_budget_folded_into_inquiry(self):
        result = parse_thumbtack(_parts(body=THUMBTACK_BODY))
        assert result.inquiry_text is not None
        assert "Budget" in result.inquiry_text or "$150" in result.inquiry_text

    def test_budget_scanned_from_body_when_no_label(self):
        body = "Name: Tim\nEmail: tim@example.com\nI need a manager, willing to pay $200/mo"
        result = parse_thumbtack(_parts(body=body))
        assert result.inquiry_text is not None and "$200" in result.inquiry_text


# ── Dispatcher: matching ──────────────────────────────────────────────────────

class TestClassifyAndParse:
    def test_apm_sender_matches(self):
        result = classify_and_parse(_parts(
            sender="APM Leads <leads@allpropertymanagement.com>",
            body=APM_BODY,
        ))
        assert result.source_channel == "APM"

    def test_mmp_sender_matches(self):
        result = classify_and_parse(_parts(
            sender="notify@managemyproperty.com",
            body=MMP_BODY,
        ))
        assert result.source_channel == "MANAGE_MY_PROPERTY"

    def test_thumbtack_sender_matches(self):
        result = classify_and_parse(_parts(
            sender="no-reply@thumbtack.com",
            body=THUMBTACK_BODY,
        ))
        assert result.source_channel == "THUMBTACK"

    # ── UNCLASSIFIED fallback ─────────────────────────────────────────────────

    def test_unknown_sender_unclassified(self):
        result = classify_and_parse(_parts(
            sender="unknown@randomplat.com",
            subject="New Lead",
            body="Someone wants to contact you.",
        ))
        assert result.source_channel == UNCLASSIFIED
        assert result.requires_human_review is True

    def test_unclassified_does_not_carry_fallback_email(self):
        """PR finding: an unmatched notification must NOT adopt the From-header
        address as the prospect email — for a portal that is the portal itself.
        The row stays review-required and the human supplies the real email."""
        result = classify_and_parse(_parts(
            sender="other@portal.io",
            body="lead body",
        ))
        assert result.source_channel == UNCLASSIFIED
        assert result.requires_human_review is True
        assert result.email is None

    def test_unclassified_not_dropped(self):
        # Must return a ParsedLead, never raise
        result = classify_and_parse(_parts(sender="", subject="", body=""))
        assert isinstance(result, ParsedLead)
        assert result.requires_human_review is True

    def test_crashing_parser_falls_through_to_unclassified(self, monkeypatch):
        from src.services import portal_parsers

        def _boom(parts):
            raise RuntimeError("simulated drift")

        # Patch the APM parser to blow up
        original = portal_parsers.PORTAL_REGISTRY[0].parser
        monkeypatch.setattr(portal_parsers.PORTAL_REGISTRY[0], "parser", _boom)
        try:
            result = classify_and_parse(_parts(
                sender="leads@allpropertymanagement.com",
                body=APM_BODY,
            ))
            assert result.source_channel == UNCLASSIFIED
            assert result.requires_human_review is True
        finally:
            monkeypatch.setattr(portal_parsers.PORTAL_REGISTRY[0], "parser", original)

    # ── Config-driven proof: registry order matters ───────────────────────────

    def test_first_matching_config_wins(self):
        # A sender matching APM should NOT be parsed by MMP even if both would match.
        result = classify_and_parse(_parts(
            sender="leads@allpropertymanagement.com",
            body=APM_BODY,
        ))
        assert result.source_channel == "APM"

    # ── Security: display-name spoofing must never trigger auto-dispatch ──────

    def test_spoofed_display_name_does_not_match_portal(self):
        """PR review finding: matching on the raw From string let an attacker
        spoof 'From: Thumbtack <attacker@evil.example>' with a crafted body
        (Name/Email lines pointing at a victim) and get auto-dispatched as a
        real Thumbtack lead. Classification must key off the real address
        domain only — never the attacker-controlled display name — so this
        falls through to UNCLASSIFIED/human-review instead."""
        result = classify_and_parse(_parts(
            sender="Thumbtack <attacker@evil.example>",
            subject="New lead",
            body="Name: Victim\nEmail: victim@example.com\nPhone: 555-000-1111",
        ))
        assert result.source_channel == UNCLASSIFIED
        assert result.requires_human_review is True
        assert result.email is None

    def test_spoofed_display_name_apm_and_mmp_also_rejected(self):
        for sender in (
            "All Property Management <attacker@evil.example>",
            "Manage My Property <attacker@evil.example>",
        ):
            result = classify_and_parse(_parts(sender=sender, body="Name: X\nEmail: x@example.com"))
            assert result.source_channel == UNCLASSIFIED, sender
            assert result.requires_human_review is True, sender

    def test_subdomain_of_real_portal_domain_still_matches(self):
        result = classify_and_parse(_parts(
            sender="notify@mail.thumbtack.com",
            body=THUMBTACK_BODY,
        ))
        assert result.source_channel == "THUMBTACK"

    def test_lookalike_domain_does_not_match(self):
        # "thumbtack.com.evil.example" is NOT a subdomain of thumbtack.com.
        result = classify_and_parse(_parts(
            sender="notify@thumbtack.com.evil.example",
            body=THUMBTACK_BODY,
        ))
        assert result.source_channel == UNCLASSIFIED

    def test_phone_only_lead_never_adopts_portal_from_header(self):
        """PR finding: a name+phone APM lead with no body email is 'usable'
        (not review-required), but its email must stay None — never the portal's
        own From-header address, which would make the STL sweep auto-reply to
        the portal instead of the owner."""
        body = "Owner Name: Sam\nPhone: 555-321-9876\nMessage: hi"
        result = classify_and_parse(_parts(
            sender="leads@allpropertymanagement.com",
            body=body,
        ))
        assert result.source_channel == "APM"
        assert result.prospect_name == "Sam"
        assert result.phone is not None and "555" in result.phone
        assert result.requires_human_review is False  # name + phone = usable
        assert result.email is None                    # but no portal backfill


# ── PR finding: HTML-only notifications + no portal-sender email fallback ─────

APM_HTML_ONLY = (
    "<html><body>"
    "<p>Owner Name: Jane Smith</p>"
    "<p>Email: jane@ownermail.com</p>"
    "<p>Phone: (813) 555-0100</p>"
    "<p>Property Address: 12 Bay St, Tampa FL 33602</p>"
    "<p>Message: I own several units and need management.</p>"
    "</body></html>"
)


class TestHtmlOnlyAndEmailFallback:
    def test_html_only_apm_is_parsed_not_lost_to_review(self):
        """An HTML-only APM notification (blank body_plain) must still extract
        the owner's labelled details rather than degrade to human review."""
        result = classify_and_parse(_parts(
            sender="APM Leads <leads@allpropertymanagement.com>",
            subject="New lead from All Property Management",
            body="",                      # no text/plain part
            html=APM_HTML_ONLY,
        ))
        assert result.source_channel == "APM"
        assert result.prospect_name == "Jane Smith"
        assert result.email == "jane@ownermail.com"
        assert result.phone == "(813) 555-0100"
        assert result.requires_human_review is False

    def test_unclassified_does_not_seed_email_from_portal_sender(self):
        """An unmatched notification must NOT carry the From-header address as
        the prospect email — for a portal that is the portal's own address."""
        result = classify_and_parse(_parts(
            sender="notifications@someportal.com",
            subject="You have a new lead",
            body="opaque body with no labels",
        ))
        assert result.source_channel == UNCLASSIFIED
        assert result.requires_human_review is True
        assert result.email is None

    def test_review_required_parse_does_not_backfill_portal_email(self):
        """A matched-but-unparseable APM email (no owner fields) stays review-
        required and must not adopt the From-header (portal) address."""
        result = classify_and_parse(_parts(
            sender="leads@allpropertymanagement.com",
            subject="New lead",
            body="(nothing parseable here)",
        ))
        assert result.source_channel == "APM"
        assert result.requires_human_review is True
        assert result.email is None
