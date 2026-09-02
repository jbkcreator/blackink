"""Ghost Shopper — LangGraph node functions.

Node execution order per crawl iteration:

  fetch_and_extract
        │
  form_validator ──(valid)──► fill_and_submit ──► confirm_check
        │                                               │
        └──(no valid form)──► queue_ranker ◄────────────┘ (no confirmation)
                                    │
                              loop_controller
                                    │
                       ┌────────────┴────────────┐
                  continue loop             terminate
                      │                        │
              fetch_and_extract              END

Playwright import is deferred inside each node so the module can be imported
without playwright installed (unit tests, graph compile checks).

Fixes applied:
  - fetch_and_extract: wait_until="networkidle" + explicit form wait for JS-rendered pages
  - fill_and_submit:   captures post_submit_url + post_submit_body before browser closes
  - confirm_check:     reads post_submit_url/body (no re-fetch); checks URL patterns + text
  - _try_fill:         handles split first/last name fields; avoids company/last name fields
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any
from urllib.parse import urljoin, urlparse

from src.agents.ink.subagents.ghost_shopper.prompts import (
    FORM_VALIDATOR_SYSTEM,
    FORM_VALIDATOR_USER,
    QUEUE_RANKER_SYSTEM,
    QUEUE_RANKER_USER,
)
from src.agents.ink.subagents.ghost_shopper.state import CrawlState, SUBMISSION_TEMPLATE

logger = logging.getLogger(__name__)

# ── helpers ───────────────────────────────────────────────────────────────────

def _same_domain(base: str, url: str) -> bool:
    try:
        return urlparse(base).netloc == urlparse(url).netloc
    except Exception:
        return False


def _normalize(base: str, href: str) -> str | None:
    try:
        full = urljoin(base, href)
        p = urlparse(full)
        if p.scheme not in ("http", "https"):
            return None
        if not _same_domain(base, full):
            return None
        return full.split("#")[0]
    except Exception:
        return None


def _call_llm(system: str, user: str) -> str:
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def _compress_form(form: dict) -> str:
    parts = []
    for field in form.get("fields", []):
        label = field.get("label", "")
        name  = field.get("name", "")
        ph    = field.get("placeholder", "")
        ftype = field.get("type", "text")
        parts.append(f"  [{ftype}] label={label!r} name={name!r} placeholder={ph!r}")
    action = form.get("action", "")
    method = form.get("method", "post").upper()
    return f"FORM action={action!r} method={method}\n" + "\n".join(parts)


# ── 1. FETCH & EXTRACT ────────────────────────────────────────────────────────

def node_fetch_and_extract(state: CrawlState) -> dict:
    """Playwright: fetch current_url, extract <a> hrefs and <form> elements.

    Uses networkidle wait so JS-rendered forms are present before extraction.
    Falls back to domcontentloaded if networkidle times out (SPAs with endless
    background polling would hang otherwise).
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    url = state["current_url"]
    logger.info("ghost_shopper.fetch: url=%s depth=%d", url, state["depth"])

    hrefs: list[str] = []
    forms: list[dict] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.set_extra_http_headers({"User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )})

            # networkidle ensures JS-rendered forms are in the DOM
            try:
                page.goto(url, timeout=20_000, wait_until="networkidle")
            except PWTimeout:
                # networkidle timed out (SPA with background polling) — use whatever loaded
                logger.warning(
                    "ghost_shopper.fetch: networkidle timeout, retrying with load url=%s", url
                )
                try:
                    page.goto(url, timeout=15_000, wait_until="load")
                except PWTimeout:
                    logger.warning("ghost_shopper.fetch: load timeout too url=%s", url)
                    browser.close()
                    return {
                        "visited":         list(set(state["visited"]) | {url}),
                        "current_forms":   [],
                        "post_submit_url":  None,
                        "post_submit_body": None,
                    }

            # Extra wait for lazy-rendered forms (React/Vue hydration)
            try:
                page.wait_for_selector("form", timeout=3_000)
            except Exception:
                pass  # no form appeared — proceed with whatever is in the DOM

            # Extract hrefs
            anchors = page.eval_on_selector_all(
                "a[href]",
                "els => els.map(e => ({href: e.getAttribute('href'), text: e.innerText.trim()}))",
            )
            for a in anchors:
                norm = _normalize(url, a.get("href", ""))
                if norm and norm not in state["visited"]:
                    hrefs.append(norm)

            # Extract forms — includes shadow-DOM-accessible inputs where possible
            raw_forms = page.eval_on_selector_all("form", """els => els.map(form => ({
                action: form.action,
                method: form.method,
                fields: Array.from(form.querySelectorAll('input,textarea,select')).map(f => ({
                    type:        f.type || f.tagName.toLowerCase(),
                    name:        f.name || f.id || '',
                    placeholder: f.placeholder || '',
                    label:       (() => {
                        if (f.labels && f.labels[0]) return f.labels[0].innerText.trim();
                        const forEl = f.id && document.querySelector('label[for="' + f.id + '"]');
                        if (forEl) return forEl.innerText.trim();
                        const wrap = f.closest('label');
                        return wrap ? wrap.innerText.replace(f.value || '', '').trim() : '';
                    })()
                })).filter(f => f.type !== 'hidden')
            }))""")
            forms = [f for f in raw_forms if f.get("fields")]  # skip empty/hidden forms
            browser.close()
    except Exception as exc:
        logger.error("ghost_shopper.fetch: error url=%s: %s", url, exc)

    existing_urls = {c["url"] for c in state["candidate_queue"]}
    visited_set   = set(state["visited"])
    new_candidates = [
        {"url": h, "score": 0.5, "depth": state["depth"] + 1}
        for h in hrefs
        if h not in existing_urls and h not in visited_set and h != url
    ]

    logger.info(
        "ghost_shopper.fetch: forms=%d new_candidates=%d url=%s",
        len(forms), len(new_candidates), url,
    )
    return {
        "visited":          list(visited_set | {url}),
        "candidate_queue":  state["candidate_queue"] + new_candidates,
        "current_forms":    forms,
        "form_valid":       None,
        "post_submit_url":  None,
        "post_submit_body": None,
    }


# ── 2. FORM VALIDATOR ─────────────────────────────────────────────────────────

def node_form_validator(state: CrawlState) -> dict:
    """LLM (Sonnet): decide if current_forms contains a valid owner inquiry form.

    Short-circuits without an LLM call if no forms were found on the page.
    """
    forms = state.get("current_forms", [])
    if not forms:
        logger.info("ghost_shopper.form_validator: no forms url=%s", state["current_url"])
        return {"form_valid": False}

    form_summary = "\n\n".join(_compress_form(f) for f in forms)
    user_msg = FORM_VALIDATOR_USER.format(
        url=state["current_url"],
        form_summary=form_summary,
    )

    try:
        raw    = _call_llm(FORM_VALIDATOR_SYSTEM, user_msg)
        parsed = json.loads(raw)
        valid  = bool(parsed.get("valid", False))
        logger.info(
            "ghost_shopper.form_validator: valid=%s confidence=%.2f reason=%s url=%s",
            valid, parsed.get("confidence", 0), parsed.get("reason", ""), state["current_url"],
        )
    except Exception as exc:
        logger.warning("ghost_shopper.form_validator: LLM error url=%s: %s", state["current_url"], exc)
        valid = False

    return {
        "form_valid":     valid,
        "llm_calls_used": state["llm_calls_used"] + 1,
    }


def route_after_form_validator(state: CrawlState) -> str:
    return "fill_and_submit" if state.get("form_valid") else "queue_ranker"


# ── 3. FILL & SUBMIT ──────────────────────────────────────────────────────────

# Ordered from most-specific to broadest — tried in sequence, first match wins.
_NAME_SELECTORS = [
    "input[name='name']",
    "input[name='full_name']",
    "input[name='fullname']",
    "input[name='your_name']",
    "input[id='name']",
    "input[id='full_name']",
    "input[placeholder='Full Name' i]",
    "input[placeholder='Your Name' i]",
    "input[placeholder='Name' i]",
    # broad fallback — excludes company/last/first/middle name fields
    "input[type='text'][name*='name' i]:not([name*='company' i]):not([name*='last' i])"
    ":not([name*='first' i]):not([name*='business' i])",
]
_FIRST_NAME_SELECTORS = [
    "input[name='first_name']", "input[name='firstname']",
    "input[id='first_name']",   "input[id='firstname']",
    "input[placeholder*='first name' i]", "input[placeholder*='first' i]",
]
_LAST_NAME_SELECTORS = [
    "input[name='last_name']", "input[name='lastname']",
    "input[id='last_name']",   "input[id='lastname']",
    "input[placeholder*='last name' i]", "input[placeholder*='last' i]",
]
_EMAIL_SELECTORS = [
    "input[type='email']",
    "input[name='email']",   "input[name='email_address']",
    "input[id='email']",
    "input[placeholder*='email' i]",
]
_PHONE_SELECTORS = [
    "input[type='tel']",
    "input[name='phone']",   "input[name='phone_number']",
    "input[name='telephone']", "input[name='mobile']",
    "input[id='phone']",
    "input[placeholder*='phone' i]",
]
_MESSAGE_SELECTORS = [
    "textarea[name='message']", "textarea[name='comments']",
    "textarea[name='details']", "textarea[name='notes']",
    "textarea[id='message']",   "textarea",
    "input[name='message']",
]


def node_fill_and_submit(state: CrawlState) -> dict:
    """Playwright: fill the valid form with SUBMISSION_TEMPLATE and submit.

    Captures post_submit_url and post_submit_body *before* closing the browser
    so confirm_check doesn't need to re-fetch (and can check the redirect page).
    Handles split first/last name fields separately from combined name.
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    url  = state["current_url"]
    tmpl = SUBMISSION_TEMPLATE
    logger.info("ghost_shopper.fill_and_submit: url=%s", url)

    submitted_at: int | None     = None
    post_submit_url:  str | None = None
    post_submit_body: str | None = None
    success = False

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=20_000, wait_until="networkidle")

            first, last = tmpl["name"].split(" ", 1)

            # Try split first/last fields first; fall back to combined name
            filled_first = _fill_first(page, _FIRST_NAME_SELECTORS, first)
            filled_last  = _fill_first(page, _LAST_NAME_SELECTORS, last)
            if not (filled_first and filled_last):
                _fill_first(page, _NAME_SELECTORS, tmpl["name"])

            _fill_first(page, _EMAIL_SELECTORS, tmpl["email"])
            _fill_first(page, _PHONE_SELECTORS, tmpl["phone"])
            _fill_first(page, _MESSAGE_SELECTORS, tmpl["message"])

            submit_btn = page.query_selector(
                "button[type='submit'], input[type='submit'], "
                "button:has-text('Submit'), button:has-text('Send'), "
                "button:has-text('Get Started'), button:has-text('Contact Us')"
            )
            if submit_btn:
                submit_btn.click()
                submitted_at = int(time.time() * 1000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10_000)
                except PWTimeout:
                    page.wait_for_load_state("load", timeout=5_000)
                post_submit_url  = page.url
                post_submit_body = page.inner_text("body").lower()
                success = True
                logger.info(
                    "ghost_shopper.fill_and_submit: submitted url=%s → post_url=%s",
                    url, post_submit_url,
                )
            else:
                logger.warning("ghost_shopper.fill_and_submit: no submit button found url=%s", url)

            browser.close()
    except Exception as exc:
        logger.error("ghost_shopper.fill_and_submit: error url=%s: %s", url, exc)

    return {
        "submitted_at":    submitted_at if success else None,
        "post_submit_url":  post_submit_url,
        "post_submit_body": post_submit_body,
        "result":          "PENDING",
    }


def _fill_first(page: Any, selectors: list[str], value: str) -> bool:
    """Try each selector in order; fill the first match. Returns True if filled."""
    for sel in selectors:
        try:
            el = page.query_selector(sel)
            if el and el.is_visible():
                el.fill(value)
                return True
        except Exception:
            continue
    return False


# ── 4. CONFIRM CHECK ──────────────────────────────────────────────────────────

_CONFIRM_TEXT_PATTERNS = [
    "thank you", "thanks!", "we'll be in touch", "we will be in touch",
    "message received", "form submitted", "submission received",
    "we received your", "someone will contact", "will reach out",
    "request received", "inquiry received", "we'll get back",
    "we will get back", "shortly", "within 24", "within one business",
]
_CONFIRM_URL_PATTERNS = [
    "/thank", "/thanks", "/success", "/confirm", "/confirmation",
    "/submitted", "/done", "/received", "thank-you", "thank_you",
    "success=true", "submitted=true",
]


def node_confirm_check(state: CrawlState) -> dict:
    """Rule-based: confirm submission using post_submit_url and post_submit_body.

    These were captured inside fill_and_submit before the browser closed — no
    re-fetch needed. Checks URL redirect patterns first (fast), then body text.

    If submitted_at is None (submit step failed), return PENDING immediately
    so loop_controller / queue_ranker can try another URL.
    """
    if not state.get("submitted_at"):
        logger.info(
            "ghost_shopper.confirm_check: no submitted_at — submit failed url=%s",
            state["current_url"],
        )
        return {"result": "PENDING"}

    post_url  = state.get("post_submit_url") or ""
    post_body = state.get("post_submit_body") or ""

    # 1. URL changed and contains a confirmation path segment
    url_confirmed = any(p in post_url.lower() for p in _CONFIRM_URL_PATTERNS)
    # 2. Body contains confirmation language
    text_confirmed = any(p in post_body for p in _CONFIRM_TEXT_PATTERNS)

    if url_confirmed:
        logger.info(
            "ghost_shopper.confirm_check: URL pattern matched post_url=%s", post_url
        )
        return {"result": "SUBMITTED"}

    if text_confirmed:
        logger.info(
            "ghost_shopper.confirm_check: text pattern matched url=%s", state["current_url"]
        )
        return {"result": "SUBMITTED"}

    # submitted_at is set but no hard confirmation signal — treat as SUBMITTED.
    # Many PM sites redirect to homepage or show inline JS success without
    # a persistent confirmation page. A false-negative (missed submission) is
    # worse than a false-positive (treating an ambiguous post-submit as confirmed).
    logger.info(
        "ghost_shopper.confirm_check: no confirmation signal — treating as SUBMITTED "
        "post_url=%s", post_url,
    )
    return {"result": "SUBMITTED"}


def route_after_confirm_check(state: CrawlState) -> str:
    if state.get("result") == "SUBMITTED":
        return "__end__"
    return "queue_ranker"


# ── 5. QUEUE RANKER ───────────────────────────────────────────────────────────

def node_queue_ranker(state: CrawlState) -> dict:
    """LLM (Sonnet): re-rank candidate_queue by likelihood of owner inquiry form.

    Skips LLM call and sorts by existing scores if budget is exhausted.
    """
    queue = state["candidate_queue"]
    if not queue:
        return {}

    remaining_llm = state["max_llm_calls"] - state["llm_calls_used"]
    if remaining_llm <= 0:
        logger.info("ghost_shopper.queue_ranker: LLM budget exhausted — sorting by score")
        return {"candidate_queue": sorted(queue, key=lambda c: c["score"], reverse=True)}

    candidates_text = "\n".join(
        f"  score={c['score']:.1f} depth={c['depth']} url={c['url']}"
        for c in queue[:20]
    )
    user_msg = QUEUE_RANKER_USER.format(
        current_url=state["current_url"],
        remaining_llm_calls=remaining_llm,
        remaining_depth=state["max_depth"] - state["depth"],
        candidates=candidates_text,
    )

    try:
        raw    = _call_llm(QUEUE_RANKER_SYSTEM, user_msg)
        ranked = json.loads(raw)
        score_map = {item["url"]: item["score"] for item in ranked}
        updated = sorted(
            [{**c, "score": score_map.get(c["url"], c["score"])} for c in queue],
            key=lambda c: c["score"],
            reverse=True,
        )
        logger.info(
            "ghost_shopper.queue_ranker: re-ranked %d candidates url=%s",
            len(updated), state["current_url"],
        )
        return {
            "candidate_queue": updated,
            "llm_calls_used":  state["llm_calls_used"] + 1,
        }
    except Exception as exc:
        logger.warning("ghost_shopper.queue_ranker: LLM error: %s", exc)
        return {}


# ── 6. LOOP CONTROLLER ────────────────────────────────────────────────────────

def node_loop_controller(state: CrawlState) -> dict:
    """Rule-based: check termination; pop next URL from queue if continuing."""
    queue = state["candidate_queue"]
    depth = state["depth"]

    if not queue:
        logger.info("ghost_shopper.loop_controller: queue empty — FORM_NOT_FOUND")
        return {"result": "FORM_NOT_FOUND"}

    if depth >= state["max_depth"]:
        logger.info("ghost_shopper.loop_controller: max_depth=%d reached — FORM_NOT_FOUND", state["max_depth"])
        return {"result": "FORM_NOT_FOUND"}

    if state["llm_calls_used"] >= state["max_llm_calls"]:
        logger.info("ghost_shopper.loop_controller: LLM budget exhausted — FORM_NOT_FOUND")
        return {"result": "FORM_NOT_FOUND"}

    next_candidate, *remaining = queue
    logger.info(
        "ghost_shopper.loop_controller: next url=%s score=%.2f depth=%d",
        next_candidate["url"], next_candidate["score"], depth + 1,
    )
    return {
        "current_url":     next_candidate["url"],
        "candidate_queue": remaining,
        "depth":           depth + 1,
        "current_forms":   [],
        "form_valid":      None,
        "post_submit_url":  None,
        "post_submit_body": None,
    }


def route_after_loop_controller(state: CrawlState) -> str:
    if state.get("result") in ("FORM_NOT_FOUND", "ERROR"):
        return "__end__"
    return "fetch_and_extract"
