"""Owner Score Self-Serve Landing Page (Subtask 3.2.3) — the one
deliberately unauthenticated router in this API. Every other router
(webhooks included) is token/HMAC-gated; this one is meant to be
reachable by any anonymous visitor.

The POST handler's entire job is to validate and insert one row into
self_serve_audit_submissions (see migrations/apply_self_serve_audit_submissions.py)
— no live company lookup, no OVS scoring, no Google Places call in the
request path. src/tasks/self_serve_audit_worker.py does all of that,
out-of-band. This is what makes the response never expose whether a
company already exists: the HTTP response never depends on prior state,
only on whether the submitted domain passed validation.

Deployment note: /audit reachable on this service's own URL is what can
be verified from code. audit.getblackink.com itself requires a DNS
record and a Cloud Run domain mapping done outside this codebase — see
CLAUDE.md's deployment section. That DoD line stays open until done.
"""

from __future__ import annotations

import hashlib
import json
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import get_settings
from src.core.database import get_db_context
from src.core.redis_client import get_redis_client
from src.loaders.base import BaseIngestLoader
from src.services.booking_link import resolve_booking_link
from src.services.owner_visibility.signals.website import _is_safe_host

router = APIRouter(tags=["public-landing"])

_INTERNAL_SALES_CLIENT_ID = "BLACKINK_INTERNAL_SALES"
_INVALID_HOST_SUFFIXES = (".local",)
_INVALID_HOSTS = {"localhost"}
_RATE_LIMIT_WINDOW_SECONDS = 600


class AuditSubmission(BaseModel):
	domain: str = Field(..., min_length=3, max_length=255)
	# Required, not optional -- a domain-only submission carries no lead
	# and no company_name to score against (see self_serve_audit_worker.py's
	# use of visitor_company). FastAPI/pydantic rejects a missing/invalid
	# field with 422 automatically, before this handler ever runs.
	name: str = Field(..., min_length=1, max_length=200)
	email: EmailStr
	company: str = Field(..., min_length=1, max_length=200)
	# Required -- companies.county_slug and owner_visibility_scores.county_slug
	# are both NOT NULL, and this repo has no domain-to-county geocoding step,
	# so a self-serve submission with no county can never be scored (see
	# self_serve_audit_worker.py). The landing page's dropdown is populated
	# from the real, currently-launched counties (GET /api/v1/public/counties),
	# never free text.
	county_slug: str = Field(..., min_length=1, max_length=60)
	website_url: Optional[str] = ""  # honeypot — a real visitor never fills this in


class _RateLimitUnavailable(Exception):
	"""Redis could not be reached to check the rate limit — the caller
	must fail closed (reject the request), never silently allow unlimited
	submissions just because the limiter itself is down."""


def _check_rate_limit(request: Request) -> bool:
	"""Returns True if the request is over the limit. Raises
	_RateLimitUnavailable if Redis itself couldn't be reached -- this
	must never be swallowed into "not limited", or a Redis outage would
	remove all rate limiting from a public, cost-incurring endpoint."""
	ip = (request.client.host if request.client else "") or "unknown"
	key = f"landing:ratelimit:{hashlib.sha256(ip.encode()).hexdigest()}"
	try:
		r = get_redis_client()
		count = r.incr(key)
		if count == 1:
			r.expire(key, _RATE_LIMIT_WINDOW_SECONDS)
	except Exception as exc:
		raise _RateLimitUnavailable(str(exc)) from exc
	return count > get_settings().self_serve_rate_limit_per_10min


def _get_db():
	"""Scoped to the reserved BLACKINK_INTERNAL_SALES client (never a real
	paying tenant's client_id) -- not deps.get_db()'s bare, unscoped
	session. This router's own event log INSERT (events is RLS-protected,
	direct client_id mode) would otherwise fail its RLS WITH CHECK clause,
	since app.current_client_id is never set on an unscoped session.
	self_serve_audit_submissions itself carries no RLS policy at all (see
	its migration), so scoping this session doesn't affect it either way."""
	with get_db_context(client_id=_INTERNAL_SALES_CLIENT_ID) as session:
		yield session


def _is_ip_literal(host: str) -> bool:
	import ipaddress

	try:
		ipaddress.ip_address(host)
		return True
	except ValueError:
		return False


def _validate_domain(domain_normalized: str) -> bool:
	"""True only if the domain is safe to eventually fetch — never a raw
	IP literal, never localhost/*.local, and every resolved address is
	public (reuses website.py's SSRF guard, the same check the eventual
	OVS website-signal fetch performs again at fetch time)."""
	if not domain_normalized or "." not in domain_normalized:
		return False
	if _is_ip_literal(domain_normalized):
		return False
	if domain_normalized in _INVALID_HOSTS or domain_normalized.endswith(_INVALID_HOST_SUFFIXES):
		return False
	return _is_safe_host(domain_normalized)


@router.get("/api/v1/public/counties")
def list_public_counties(db: Session = Depends(_get_db)):
	"""Real reference data (county_slug/county_name), read-only, no PII --
	backs the landing page's required county dropdown. counties carries no
	RLS policy (global reference data, see config/tenant_policies.py's
	comment), so this is safe to expose to any anonymous visitor."""
	rows = db.execute(text("SELECT county_slug, county_name FROM counties ORDER BY county_name")).fetchall()
	return [{"county_slug": r.county_slug, "county_name": r.county_name} for r in rows]


@router.post("/api/v1/public/owner-score-audit")
def submit_owner_score_audit(payload: AuditSubmission, request: Request, db: Session = Depends(_get_db)):
	"""`ok` in the response tells the landing page's JS whether to fire the
	tracking pixels (accepted-for-processing vs rejected/honeypot/rate-
	limited) — it never reveals whether the company already existed or was
	already scored, which is the one thing this response must never leak
	(the DB insert and event log always happen identically regardless of
	prior company state)."""
	try:
		if _check_rate_limit(request):
			return JSONResponse(status_code=429, content={"ok": False, "error": "rate_limited"})
	except _RateLimitUnavailable:
		# Fail CLOSED: if the limiter itself can't be reached, reject
		# rather than silently permit unlimited scoring-triggering
		# requests. No DB write, no worker cost incurred.
		return JSONResponse(status_code=503, content={"ok": False, "error": "temporarily_unavailable"})

	domain_normalized = BaseIngestLoader.normalize_domain(payload.domain)
	is_honeypot = bool((payload.website_url or "").strip())
	county_exists = db.execute(
		text("SELECT 1 FROM counties WHERE county_slug = :slug"), {"slug": payload.county_slug}
	).first() is not None
	is_valid_domain = _validate_domain(domain_normalized) and not is_honeypot and county_exists
	status = "PENDING" if is_valid_domain else "REJECTED_DOMAIN"

	ip = (request.client.host if request.client else "") or ""
	ip_hash = hashlib.sha256(ip.encode()).hexdigest() if ip else None

	submission_id = db.execute(
		text(
			"INSERT INTO self_serve_audit_submissions "
			"(domain_submitted, domain_normalized, visitor_name, visitor_email, visitor_company, "
			" county_slug, submitted_ip_hash, status) "
			"VALUES (:domain_submitted, :domain_normalized, :name, :email, :company, "
			" :county_slug, :ip_hash, :status) "
			"RETURNING submission_id"
		),
		{
			"domain_submitted": payload.domain, "domain_normalized": domain_normalized,
			"name": payload.name, "email": payload.email, "company": payload.company,
			"county_slug": payload.county_slug if county_exists else None,
			"ip_hash": ip_hash, "status": status,
		},
	).scalar()

	db.execute(
		text(
			"INSERT INTO events (client_id, event_type, entity_type, entity_id, payload) "
			"VALUES (:client_id, 'owner_score_self_serve_triggered', 'self_serve_audit_submission', "
			":entity_id, :payload)"
		),
		{
			"client_id": _INTERNAL_SALES_CLIENT_ID, "entity_id": str(submission_id),
			"payload": json.dumps({"name": payload.name, "email": payload.email, "company": payload.company}),
		},
	)

	link = resolve_booking_link(db, name=payload.name, email=payload.email)
	return {
		"ok": is_valid_domain,
		"redirect_url": link.url if link else None,
		"prefilled": link.prefilled if link else False,
	}


_LANDING_PAGE_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Owner Visibility Score — getblackink.com</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 0; padding: 2rem 1.25rem; background: #f6f5f2; color: #1a1a1a; }
  .card { max-width: 480px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 2rem 1.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.08); }
  h1 { font-size: 1.4rem; margin: 0 0 .5rem; }
  p { color: #555; line-height: 1.5; }
  label { display: block; font-weight: 600; margin: 1rem 0 .25rem; font-size: .9rem; }
  input, select { width: 100%; box-sizing: border-box; padding: .6rem .7rem; border: 1px solid #ccc; border-radius: 8px; font-size: 1rem; }
  button { width: 100%; margin-top: 1.25rem; padding: .8rem; border: none; border-radius: 8px; background: #1a1a1a; color: #fff; font-size: 1rem; cursor: pointer; }
  .hp { position: absolute; left: -9999px; }
  #status { margin-top: 1rem; font-size: .9rem; }
</style>
</head>
<body>
<div class="card">
  <h1>Check your firm's public Owner Visibility Score</h1>
  <p>Enter your company's domain to get a free, no-obligation score.</p>
  <form id="audit-form">
    <label for="domain">Company domain</label>
    <input id="domain" name="domain" type="text" placeholder="yourcompany.com" required minlength="3">
    <label for="name">Your name</label>
    <input id="name" name="name" type="text" required>
    <label for="email">Work email</label>
    <input id="email" name="email" type="email" required>
    <label for="company">Company name</label>
    <input id="company" name="company" type="text" required>
    <label for="county">County</label>
    <select id="county" name="county" required>
      <option value="" disabled selected>Select your county</option>
    </select>
    <input class="hp" id="website_url" name="website_url" type="text" tabindex="-1" autocomplete="off">
    <button type="submit">Get my score</button>
  </form>
  <div id="status"></div>
</div>
<script>
(function () {
  var META_PIXEL_ID = __META_PIXEL_ID__;
  var GOOGLE_TAG_ID = __GOOGLE_TAG_ID__;

  fetch('/api/v1/public/counties').then(function (r) { return r.json(); }).then(function (counties) {
    var select = document.getElementById('county');
    counties.forEach(function (c) {
      var opt = document.createElement('option');
      opt.value = c.county_slug;
      opt.textContent = c.county_name;
      select.appendChild(opt);
    });
  }).catch(function () { /* dropdown stays empty; form submit will 422 without a county */ });
  // Short bounded wait after firing the pixel calls before navigating
  // away — the injected fbevents.js/gtag.js scripts load and send their
  // beacon asynchronously, and navigating immediately can cancel that
  // in-flight request before the browser ever sends it. Bounded so a
  // slow/blocked pixel load never holds up a real visitor for long.
  // NOTE: proving "the pixel request is sent before navigation" is a
  // browser-level (Network-tab / Playwright-style) assertion — this repo
  // has no JS test runner anywhere in it (Python-only test suite), so
  // this is verified manually per CLAUDE.md's UI-testing convention,
  // not by an automated test added here.
  var PIXEL_FLUSH_DELAY_MS = 400;

  function fireConversionPixels() {
    // Loaded and fired ONLY here, after a confirmed successful
    // submission (response.ok && data.ok) — never on page load, never
    // for a rejected/honeypot/rate-limited submission, never with any
    // submitted PII in the event payload.
    var firedAny = false;
    if (META_PIXEL_ID) {
      firedAny = true;
      var fbScript = document.createElement('script');
      fbScript.textContent =
        "!function(f,b,e,v,n,t,s){if(f.fbq)return;n=f.fbq=function(){n.callMethod?" +
        "n.callMethod.apply(n,arguments):n.queue.push(arguments)};if(!f._fbq)f._fbq=n;" +
        "n.push=n;n.loaded=!0;n.version='2.0';n.queue=[];t=b.createElement(e);t.async=!0;" +
        "t.src=v;s=b.getElementsByTagName(e)[0];s.parentNode.insertBefore(t,s)}(window,document," +
        "'script','https://connect.facebook.net/en_US/fbevents.js');" +
        "fbq('init','" + META_PIXEL_ID + "');fbq('track','Lead');";
      document.head.appendChild(fbScript);
    }
    if (GOOGLE_TAG_ID) {
      firedAny = true;
      var gtagSrc = document.createElement('script');
      gtagSrc.async = true;
      gtagSrc.src = 'https://www.googletagmanager.com/gtag/js?id=' + GOOGLE_TAG_ID;
      document.head.appendChild(gtagSrc);
      var gtagInit = document.createElement('script');
      gtagInit.textContent =
        "window.dataLayer=window.dataLayer||[];function gtag(){dataLayer.push(arguments);}" +
        "gtag('js',new Date());gtag('config','" + GOOGLE_TAG_ID + "');gtag('event','generate_lead');";
      document.head.appendChild(gtagInit);
    }
    return firedAny;
  }

  function proceed(statusEl, redirectUrl) {
    if (redirectUrl) {
      window.location = redirectUrl;
    } else {
      statusEl.textContent = "Thanks — we'll follow up by email with your score and a time to talk.";
    }
  }

  document.getElementById('audit-form').addEventListener('submit', function (e) {
    e.preventDefault();
    var statusEl = document.getElementById('status');
    statusEl.textContent = 'Submitting...';
    fetch('/api/v1/public/owner-score-audit', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        domain: document.getElementById('domain').value,
        name: document.getElementById('name').value,
        email: document.getElementById('email').value,
        company: document.getElementById('company').value,
        county_slug: document.getElementById('county').value,
        website_url: document.getElementById('website_url').value
      })
    }).then(function (r) {
      return r.json().then(function (data) { return { httpOk: r.ok, data: data }; });
    }).then(function (result) {
      var data = result.data;
      if (result.httpOk && data.ok) {
        var firedAny = fireConversionPixels();
        setTimeout(function () { proceed(statusEl, data.redirect_url); }, firedAny ? PIXEL_FLUSH_DELAY_MS : 0);
      } else if (data.error === 'rate_limited' || data.error === 'temporarily_unavailable') {
        statusEl.textContent = 'Too many requests right now — please try again shortly.';
      } else {
        statusEl.textContent = 'Please double-check your domain and try again.';
      }
    }).catch(function () {
      statusEl.textContent = 'Something went wrong — please try again.';
    });
  });
})();
</script>
</body>
</html>
"""


@router.get("/audit", response_class=HTMLResponse)
def audit_landing_page() -> HTMLResponse:
	# .replace(), not %-style formatting -- the CSS above is full of
	# literal '%' characters (width: 100%, etc.) that %-formatting would
	# try to interpret as format specifiers and crash on.
	settings = get_settings()
	html = _LANDING_PAGE_HTML.replace(
		"__META_PIXEL_ID__", json.dumps(settings.meta_pixel_id)
	).replace(
		"__GOOGLE_TAG_ID__", json.dumps(settings.google_tag_id)
	)
	return HTMLResponse(content=html)
