"""Ghost Shopper — entry point called by Ink's node_ghost_shopper.

Handles:
  - First run:  invoke with initial CrawlState
  - Crash retry: detect in-progress checkpoint → resume from last completed node
  - Clean completion: return GhostResult to Ink
"""
from __future__ import annotations

import logging
from typing import Optional

from langgraph.types import Command

from src.agents.ink.subagents.ghost_shopper.state import CrawlState, GhostResult

logger = logging.getLogger(__name__)

_THREAD_PREFIX = "gs"


def _thread_id(work_order_id: str) -> str:
    return f"{_THREAD_PREFIX}:{work_order_id}"


def run(
    graph,
    company_id:    str,
    work_order_id: str,
    website_url:   str,
) -> GhostResult:
    """Invoke or resume the Ghost Shopper graph and return the outcome.

    Called from Ink's node_ghost_shopper. `graph` is the compiled Ghost Shopper
    LangGraph instance (built once in worker.py, passed via closure).

    Crash recovery: if a checkpoint exists for this work_order_id and the graph
    is not yet at END (state.next is non-empty), we resume from the last
    completed node rather than restarting from the homepage.
    """
    config = {"configurable": {"thread_id": _thread_id(work_order_id)}}

    # ── Detect existing in-progress checkpoint ─────────────────────────────────
    try:
        existing = graph.get_state(config)
    except Exception as exc:
        logger.warning(
            "ghost_shopper.runner: get_state failed work_order_id=%s: %s — starting fresh",
            work_order_id, exc,
        )
        existing = None

    if existing and existing.next:
        logger.info(
            "ghost_shopper.runner: resuming from checkpoint work_order_id=%s next_nodes=%s",
            work_order_id, existing.next,
        )
        try:
            final_state = graph.invoke(Command(resume=None), config=config)
            return _to_result(final_state)
        except Exception as exc:
            logger.error(
                "ghost_shopper.runner: resume failed work_order_id=%s: %s",
                work_order_id, exc, exc_info=True,
            )
            return GhostResult(outcome="ERROR", submitted_at=None, error=str(exc))

    # ── Fresh run ──────────────────────────────────────────────────────────────
    logger.info(
        "ghost_shopper.runner: starting fresh run company_id=%s work_order_id=%s url=%s",
        company_id, work_order_id, website_url,
    )
    initial: CrawlState = {
        "company_id":      company_id,
        "work_order_id":   work_order_id,
        "start_url":       website_url,
        "current_url":     website_url,
        "visited":         [],
        "candidate_queue": [],
        "current_forms":   [],
        "form_valid":      None,
        "depth":           0,
        "max_depth":       3,
        "max_llm_calls":   6,
        "llm_calls_used":  0,
        "submitted_at":    None,
        "post_submit_url":  None,
        "post_submit_body": None,
        "result":          "PENDING",
        "error":           None,
    }
    try:
        final_state = graph.invoke(initial, config=config)
        return _to_result(final_state)
    except Exception as exc:
        logger.error(
            "ghost_shopper.runner: run failed work_order_id=%s: %s",
            work_order_id, exc, exc_info=True,
        )
        return GhostResult(outcome="ERROR", submitted_at=None, error=str(exc))


def _to_result(state: dict) -> GhostResult:
    raw = state.get("result", "ERROR")
    if raw not in ("SUBMITTED", "FORM_NOT_FOUND", "ERROR"):
        raw = "ERROR"
    return GhostResult(
        outcome=raw,
        submitted_at=state.get("submitted_at"),
        error=state.get("error"),
    )
