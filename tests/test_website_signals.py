"""Tests for WebsiteSignalProvider — uses responses mock, no live HTTP."""
import pytest

try:
    import responses as responses_lib
    HAS_RESPONSES = True
except ImportError:
    HAS_RESPONSES = False

from src.services.owner_visibility.signals.base import SCORED, MISSING_DATA
from src.services.owner_visibility.signals.website import (
    WebsiteSignalProvider,
    _score_owner_page,
    _score_contact_info,
    _score_after_hours,
)
from bs4 import BeautifulSoup


# ── Unit tests for individual scorers (no HTTP) ──────────────────────────────

class TestScoreOwnerPage:
    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def test_owner_link_present_awards_14(self):
        soup = self._soup('<a href="/owners">For Owners</a>')
        result = _score_owner_page(soup, "https://example.com")
        assert result.points_awarded == 14
        assert result.status == SCORED

    def test_landlord_link_present_awards_14(self):
        soup = self._soup('<a href="/landlords/info">Landlords</a>')
        result = _score_owner_page(soup, "https://example.com")
        assert result.points_awarded == 14

    def test_no_owner_link_awards_0(self):
        soup = self._soup('<a href="/contact">Contact Us</a>')
        result = _score_owner_page(soup, "https://example.com")
        assert result.points_awarded == 0
        assert result.status == SCORED


class TestScoreContactInfo:
    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def test_all_three_present_awards_10(self):
        html = """
        <p>Call us: (813) 555-1234</p>
        <p>Email: info@example.com</p>
        <form><input type="text"><button>Submit</button></form>
        """
        result = _score_contact_info(self._soup(html))
        assert result.points_awarded == 10

    def test_phone_only_awards_4(self):
        result = _score_contact_info(self._soup("<p>(813) 555-1234</p>"))
        assert result.points_awarded == 4

    def test_email_only_awards_3(self):
        result = _score_contact_info(self._soup("<p>hello@pm.com</p>"))
        assert result.points_awarded == 3

    def test_none_present_awards_0(self):
        result = _score_contact_info(self._soup("<p>Welcome to our site.</p>"))
        assert result.points_awarded == 0


class TestScoreAfterHours:
    def _soup(self, html: str) -> BeautifulSoup:
        return BeautifulSoup(html, "lxml")

    def test_after_hours_text_awards_8(self):
        result = _score_after_hours(self._soup("<p>We offer after-hours service.</p>"))
        assert result.points_awarded == 8

    def test_247_awards_8(self):
        result = _score_after_hours(self._soup("<p>Available 24/7 for emergencies.</p>"))
        assert result.points_awarded == 8

    def test_no_after_hours_awards_0(self):
        result = _score_after_hours(self._soup("<p>Business hours: 9am-5pm Mon-Fri.</p>"))
        assert result.points_awarded == 0


# ── Integration-level: no domain returns MISSING_DATA ────────────────────────

class TestWebsiteSignalProviderMissingDomain:
    def test_no_domain_or_website_returns_missing(self):
        provider = WebsiteSignalProvider()
        results = provider.collect({"company_id": "abc", "company_name": "Test Co"})
        assert len(results) == 4
        assert all(r.status == MISSING_DATA for r in results)
        assert all(r.points_awarded == 0 for r in results)


# ── HTTP-mocked tests (skipped if `responses` not installed) ──────────────────

@pytest.mark.skipif(not HAS_RESPONSES, reason="pip install responses to enable HTTP-mock tests")
class TestWebsiteSignalProviderHTTP:
    def _good_html(self) -> str:
        return """
        <html>
        <head><meta name="viewport" content="width=device-width"></head>
        <body>
            <nav><a href="/owners">For Owners</a></nav>
            <p>Call: (813) 555-0000</p>
            <p>Email: hello@acme.com</p>
            <form><button>Contact</button></form>
            <p>We provide 24/7 emergency service.</p>
        </body></html>
        """

    @pytest.fixture(autouse=True)
    def _activate(self):
        import responses as r
        with r.RequestsMock() as rsps:
            rsps.add(r.GET, "https://acme.com", body=self._good_html(), status=200)
            yield rsps

    def test_full_site_scores_correctly(self):
        provider = WebsiteSignalProvider()
        results = provider.collect({"domain": "acme.com", "company_name": "Acme PM"})
        by_name = {r.signal_name: r for r in results}
        assert by_name["website_owner_page"].points_awarded == 14
        assert by_name["website_contact_info"].points_awarded == 10
        assert by_name["website_tech_health"].points_awarded == 6   # HTTPS + viewport
        assert by_name["website_after_hours"].points_awarded == 8
        assert sum(r.points_awarded for r in results) == 38
