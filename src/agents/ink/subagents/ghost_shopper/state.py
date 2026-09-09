"""Ghost Shopper — crawl state and result types.

# DEFERRED 2026-09-03 — see runner.py for full context and reactivation notes.

CrawlState is the LangGraph state for the Ghost Shopper nested graph.
It is checkpointed to Postgres after every node completion, keyed by
thread_id = "gs:{work_order_id}".

All fields must be JSON-serialisable (no sets, no dataclasses inline).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from typing_extensions import TypedDict


class CrawlState(TypedDict):
    # ── Identity ──────────────────────────────────────────────────────────────
    company_id:      str
    work_order_id:   str
    start_url:       str

    # ── Crawl cursor ──────────────────────────────────────────────────────────
    current_url:     str                  # URL being processed this iteration
    visited:         list[str]            # all URLs fetched (dedup guard)
    candidate_queue: list[dict]           # [{"url": str, "score": float, "depth": int}]

    # ── Per-iteration extraction ──────────────────────────────────────────────
    current_forms:   list[dict]           # forms extracted from current_url
    form_valid:      Optional[bool]       # set by form_validator; None before first call

    # ── Post-submit state (captured inside fill_and_submit before browser closes) ─
    post_submit_url:  Optional[str]       # final URL after form redirect
    post_submit_body: Optional[str]       # lowercased body text of post-submit page

    # ── Budget ────────────────────────────────────────────────────────────────
    depth:           int
    max_depth:       int                  # default 3
    max_llm_calls:   int                  # hard cap, default 6
    llm_calls_used:  int

    # ── Proxy state ───────────────────────────────────────────────────────────
    fetch_blocked:   bool                 # True when last fetch hit 403/timeout/bot-block
    used_proxy:      bool                 # True once proxy was tried for current_url (per-URL, reset by loop_controller)
    proxy_confirmed: bool                 # True once proxy proved it works for this job — never reset; all subsequent fetches skip direct and go straight to proxy

    # ── Result ────────────────────────────────────────────────────────────────
    submitted_at:    Optional[int]        # ms epoch, set by fill_and_submit
    result:          str                  # PENDING | SUBMITTED | FORM_NOT_FOUND | ERROR
    error:           Optional[str]


@dataclass
class GhostResult:
    """Return value from ghost_shopper.runner.run() — consumed by Ink's node."""
    outcome:      Literal["SUBMITTED", "FORM_NOT_FOUND", "ERROR"]
    submitted_at: Optional[int]
    error:        Optional[str] = None


# Fixed submission identity — the persona Ghost Shopper uses on every form.
# Email domain is the inbox the IMAP listener monitors.
SUBMISSION_TEMPLATE = {
    "name":    "Jordan Mitchell",
    "email":   "samurai@x-mail.com",
    "phone":   "(813) 555-0192",
    "address": "4821 Harborview Dr, Tampa FL 33611",
    "message": (
        "Hi, I own a single-family rental property and I'm evaluating "
        "property management companies in the area. Could someone reach "
        "out to discuss your services and fees? Best time to call is "
        "weekday afternoons."
    ),
}
