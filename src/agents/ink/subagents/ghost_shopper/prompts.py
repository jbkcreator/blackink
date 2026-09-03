"""Ghost Shopper — LLM prompt templates.

# DEFERRED 2026-09-03 — see runner.py for full context and reactivation notes.

Two LLM calls per crawl run (typical):
  1. FORM_VALIDATOR  — called once per page that has <form> elements
  2. QUEUE_RANKER    — called once per page with no valid form found

Both use Claude Sonnet. Responses are JSON only.
"""

# ── FORM VALIDATOR ────────────────────────────────────────────────────────────
#
# Input:  compressed form HTML — field names, labels, placeholders, form action
#         (stripped to < ~400 tokens — no CSS, no scripts, no non-form markup)
# Output: { "valid": bool, "confidence": float 0-1, "reason": str }
#
# Valid = a property owner can submit an inquiry about PM services here.

FORM_VALIDATOR_SYSTEM = """\
You are an analyst evaluating HTML form elements to decide whether a property \
owner could use this form to inquire about property management services.

Respond with a single JSON object and nothing else:
{
  "valid": <true|false>,
  "confidence": <0.0 to 1.0>,
  "reason": "<one sentence>"
}

Rules:
- valid=true  if the form could plausibly capture a property owner inquiry —
  has name + email (or phone) fields + a message/notes field or purpose that
  matches owner outreach (contact us, get a quote, learn more, request info).
- valid=false if the form is a tenant rental application (asks for income,
  employment history, references, move-in date), a login/account form,
  a search bar, a newsletter-only signup with no message field, or a
  payment/maintenance request form for existing tenants.
- When uncertain, prefer valid=false with low confidence rather than guessing.
"""

FORM_VALIDATOR_USER = """\
Page URL: {url}

Extracted form elements (labels → field names → placeholders):
{form_summary}

Is this a valid property owner inquiry form?
"""


# ── QUEUE RANKER ─────────────────────────────────────────────────────────────
#
# Input:  current URL + list of candidate hrefs with link text and depth
# Output: same list re-ranked by likelihood of containing an owner inquiry form
#
# Called when current page had no valid form — need to decide where to crawl next.

QUEUE_RANKER_SYSTEM = """\
You are scoring URLs on a property management company website by how likely \
each is to contain a form where a property owner can submit an inquiry about \
PM services (contact us, get a quote, owner inquiry, etc.).

Respond with a single JSON array and nothing else — the same URLs re-ranked \
from highest to lowest score:
[
  { "url": "<url>", "score": <0.0 to 1.0> },
  ...
]

Scoring guidance:
- Score HIGH (0.7–1.0): URLs or link text containing — contact, inquiry,
  owners, landlords, property-owners, get-started, get-a-quote, about-us,
  services, free-rental-analysis, management-services
- Score MEDIUM (0.4–0.6): generic pages that might have a contact section —
  home (if not already visited), about, faq, how-it-works, pricing
- Score LOW (0.0–0.3): tenant-facing, utility, or irrelevant pages —
  tenant-portal, pay-rent, maintenance, apply-now, login, blog, news,
  careers, privacy-policy, sitemap, social-media links

Return all URLs even if the score is very low — the caller decides the cutoff.
"""

QUEUE_RANKER_USER = """\
Current page: {current_url}
Remaining budget: {remaining_llm_calls} LLM calls, {remaining_depth} depth levels

Candidate URLs (link text → URL):
{candidates}

Re-rank these by likelihood of containing an owner inquiry form.
"""
