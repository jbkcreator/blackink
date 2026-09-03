"""Ink (Campaign Agent) — LangGraph node functions.

# ============================================================
# DEFERRED — Ghost Shopper, Sendspark, GIF Generator nodes
# ============================================================
# node_ghost_shopper (via make_node_ghost_shopper), node_wait_reply,
# node_sendspark, node_gif_generator are deferred as of 2026-09-03.
# They are preserved here for future reactivation.
#
# The active OVS pipeline replaces ghost_shopper + wait_reply with an
# OVS score lookup. When reactivating, restore those nodes and update
# the graph topology in graph.py.
# ============================================================

Each node receives the full GlobalState and returns a partial dict that
LangGraph merges into the state. Nodes update `stage` as a side effect for
observability; routing is driven by graph edges, not the stage field.

Stubs are clearly marked and return placeholder values so the graph can run
end-to-end in skeleton mode. When a subagent is implemented, its node is
replaced in-place — no graph topology changes required.
"""
from __future__ import annotations

import logging
from typing import Any

from langgraph.types import interrupt

from src.agents.ink.state import GlobalState, InkStage

logger = logging.getLogger(__name__)


# ── 1. Ghost Shopper ──────────────────────────────────────────────────────────

def make_node_ghost_shopper(ghost_shopper_graph):
    """Factory: returns node_ghost_shopper closed over the compiled GS graph.

    Called once in graph.py's build_graph(). Keeps the node signature clean
    (state → dict) while giving it access to the nested graph instance.
    """
    def node_ghost_shopper(state: GlobalState) -> dict:
        from sqlalchemy import text

        from src.agents.ink.subagents.ghost_shopper.runner import run as gs_run
        from src.core.database import get_db_context

        # Look up website_url from companies table
        with get_db_context(client_id=state["client_id"]) as db:
            row = db.execute(
                text("SELECT website FROM companies WHERE company_id = :cid"),
                {"cid": state["company_id"]},
            ).fetchone()
        website_url = row.website if row and row.website else None

        if not website_url:
            logger.warning(
                "ink.nodes: ghost_shopper — no website_url for company_id=%s, skipping",
                state["company_id"],
            )
            return {"ghost_result": "FORM_NOT_FOUND", "stage": InkStage.FAILED}

        result = gs_run(
            graph=ghost_shopper_graph,
            company_id=state["company_id"],
            work_order_id=state["work_order_id"],
            website_url=website_url,
        )
        logger.info(
            "ink.nodes: ghost_shopper finished — company_id=%s outcome=%s submitted_at=%s",
            state["company_id"], result.outcome, result.submitted_at,
        )

        # Persist submitted_at to contacts so the IMAP listener can compute latency_sec
        if result.outcome == "SUBMITTED" and result.submitted_at:
            try:
                with get_db_context(client_id=state["client_id"]) as db:
                    db.execute(
                        text(
                            "UPDATE contacts SET ghost_submitted_at = :ts "
                            "WHERE company_id = :cid"
                        ),
                        {"ts": result.submitted_at, "cid": state["company_id"]},
                    )
                logger.info(
                    "ink.nodes: ghost_shopper wrote ghost_submitted_at=%s company_id=%s",
                    result.submitted_at, state["company_id"],
                )
            except Exception as exc:
                logger.error(
                    "ink.nodes: ghost_shopper DB write failed company_id=%s: %s",
                    state["company_id"], exc,
                )

        return {
            "ghost_result": result.outcome,
            "submitted_at": result.submitted_at,
            "stage": InkStage.WAIT_REPLY if result.outcome == "SUBMITTED" else InkStage.FAILED,
            **({"error": result.error} if result.error else {}),
        }
    return node_ghost_shopper


def route_after_ghost(state: GlobalState) -> str:
    result = state.get("ghost_result")
    if result == "SUBMITTED":
        return "wait_reply"
    # FORM_NOT_FOUND or ERROR — exit gracefully without running proof assets
    logger.warning(
        "ink.nodes: ghost_shopper result=%s for company_id=%s — skipping proof pipeline",
        result, state["company_id"],
    )
    return "__end__"


# ── 2. Wait Reply (suspend — IMAP reply) ──────────────────────────────────────

def node_wait_reply(state: GlobalState) -> dict:
    """Suspends the graph here and checkpoints to Postgres.

    Resumes when the IMAP listener calls graph.invoke(Command(resume=data))
    with data = { "latency_sec": int, "loss_est": int }.

    If no reply arrives in 24 hours, the IMAP listener publishes a resume
    signal with { "latency_sec": None, "loss_est": None } so the campaign
    continues without audit data.
    """
    logger.info(
        "ink.nodes: wait_reply suspending — company_id=%s work_order_id=%s",
        state["company_id"], state["work_order_id"],
    )
    resume_data: dict[str, Any] = interrupt({
        "reason": "waiting_for_imap_reply",
        "company_id": state["company_id"],
        "work_order_id": state["work_order_id"],
        "submitted_at": state.get("submitted_at"),
    })
    logger.info(
        "ink.nodes: wait_reply resumed — company_id=%s latency_sec=%s loss_est=%s",
        state["company_id"],
        resume_data.get("latency_sec"),
        resume_data.get("loss_est"),
    )
    return {
        "latency_sec": resume_data.get("latency_sec"),
        "loss_est": resume_data.get("loss_est"),
        "stage": InkStage.PDF_GENERATOR,
    }


# ── 3. PDF Generator ─────────────────────────────────────────────────────────

def node_pdf_generator(state: GlobalState) -> dict:
    """
    TODO: invoke PDFGeneratorAgent(company_id, latency_sec, loss_est).
    Compiles branded 2-page loss report PDF, stores on S3, returns pdf_url.
    Revenue loss formula: Monthly Leads × (1 − e^(−0.0005 × latency_sec))
                          × (Avg Fee × 12) × Avg Door Retention
    See: Tasks/campaign_agent_architecture.md §2.1.3
    """
    logger.warning(
        "ink.nodes: pdf_generator STUB — company_id=%s latency_sec=%s",
        state["company_id"], state.get("latency_sec"),
    )
    return {
        "pdf_url": f"https://s3.example.com/stub/{state['company_id']}/loss_report.pdf",
        "stage": InkStage.ASSETS_MERGE,
    }


# ── 4. Sendspark ─────────────────────────────────────────────────────────────

def node_sendspark(state: GlobalState) -> dict:
    """
    TODO: invoke SendsparkAgent(company_id, company_name, latency_sec, loss_est).
    Calls Sendspark REST API to generate personalised video landing page.
    URL pattern: https://watch.blackink.io/v/{company_id}?company=...&speed=...&loss=...
    Skips if latency_sec or loss_est is None (logs SENDSPARK_SKIPPED_MISSING_DATA).
    See: Tasks/campaign_agent_architecture.md §2.2.1
    """
    if not state.get("latency_sec") or not state.get("loss_est"):
        logger.warning(
            "ink.nodes: sendspark SKIPPED — missing audit data company_id=%s",
            state["company_id"],
        )
        return {"video_id": None, "landing_url": None}

    logger.warning(
        "ink.nodes: sendspark STUB — company_id=%s returning placeholder",
        state["company_id"],
    )
    company_id = state["company_id"]
    return {
        "video_id": f"stub-video-{company_id}",
        "landing_url": (
            f"https://watch.blackink.io/v/{company_id}"
            f"?speed={state['latency_sec']}&loss={state['loss_est']}"
        ),
    }


# ── 5. GIF Generator ─────────────────────────────────────────────────────────

def node_gif_generator(state: GlobalState) -> dict:
    """
    TODO: invoke GIFGeneratorAgent(company_id, website_url, landing_url, latency_sec).
    Screencaps prospect website, overlays audit score, generates 600×338px
    animated GIF (max 1.5 MB). Non-blocking — falls back to static image
    if generation exceeds 60 seconds.
    See: Tasks/campaign_agent_architecture.md §2.2.2
    """
    logger.warning(
        "ink.nodes: gif_generator STUB — company_id=%s returning placeholder",
        state["company_id"],
    )
    return {
        "gif_url": f"https://s3.example.com/stub/{state['company_id']}/thumbnail.gif",
        "stage": InkStage.ASSETS_MERGE,
    }


# ── 6. Assets Merge ───────────────────────────────────────────────────────────

def node_assets_merge(state: GlobalState) -> dict:
    """Join node — runs after pdf_generator and gif_generator both complete.

    LangGraph guarantees this node does not run until all parallel branches
    (pdf_generator → here, sendspark → gif_generator → here) are finished.
    Validates that required assets are present before handing off to Cora.
    """
    missing = [k for k in ("pdf_url", "gif_url") if not state.get(k)]
    if missing:
        logger.warning(
            "ink.nodes: assets_merge — missing assets %s for company_id=%s "
            "proceeding to Cora with partial data",
            missing, state["company_id"],
        )
    logger.info(
        "ink.nodes: assets_merge complete — company_id=%s pdf=%s video=%s gif=%s",
        state["company_id"],
        bool(state.get("pdf_url")),
        bool(state.get("video_id")),
        bool(state.get("gif_url")),
    )
    return {"stage": InkStage.CORA_DISPATCH}


# ── 7. Cora Dispatch ──────────────────────────────────────────────────────────

def node_cora_dispatch(state: GlobalState) -> dict:
    """Publishes a draft.requested event to cora:drafts.

    Cora worker picks this up, generates the 5-touch email sequence using
    all proof assets, and posts drafts to Slack for human approval.
    This node is real — cora:drafts queue is already operational.
    """
    from src.agents.cora.queue import publish as cora_publish

    payload = {
        "company_id":    state["company_id"],
        "campaign_id":   state["campaign_id"],
        "work_order_id": state["work_order_id"],
        "latency_sec":   state.get("latency_sec"),
        "loss_est":      state.get("loss_est"),
        "pdf_url":       state.get("pdf_url"),
        "video_id":      state.get("video_id"),
        "landing_url":   state.get("landing_url"),
        "gif_url":       state.get("gif_url"),
    }
    message_id = cora_publish(
        event_type="draft.requested",
        client_id=state["client_id"],
        payload=payload,
    )
    logger.info(
        "ink.nodes: cora_dispatch — published draft.requested company_id=%s message_id=%s",
        state["company_id"], message_id,
    )
    return {
        "draft_message_id": message_id,
        "stage": InkStage.WAIT_APPROVE,
    }


# ── 8. Wait Approve (suspend — Slack approval) ────────────────────────────────

def node_wait_approve(state: GlobalState) -> dict:
    """Suspends the graph here and checkpoints to Postgres.

    Resumes when the Slack approval webhook calls graph.invoke(Command(resume=data))
    with data = { "approved": bool, "approved_by": str }.

    If rejected, the graph exits without dispatching to Relay.
    """
    logger.info(
        "ink.nodes: wait_approve suspending — company_id=%s draft_message_id=%s",
        state["company_id"], state.get("draft_message_id"),
    )
    resume_data: dict[str, Any] = interrupt({
        "reason": "waiting_for_slack_approval",
        "company_id": state["company_id"],
        "work_order_id": state["work_order_id"],
        "draft_message_id": state.get("draft_message_id"),
    })
    approved = bool(resume_data.get("approved"))
    logger.info(
        "ink.nodes: wait_approve resumed — company_id=%s approved=%s",
        state["company_id"], approved,
    )
    return {
        "stage": InkStage.RELAY_DISPATCH if approved else InkStage.FAILED,
    }


def route_after_approve(state: GlobalState) -> str:
    if state.get("stage") == InkStage.RELAY_DISPATCH:
        return "relay_dispatch"
    return "__end__"


# ── 9. Relay Dispatch ─────────────────────────────────────────────────────────

def node_relay_dispatch(state: GlobalState) -> dict:
    """
    TODO: publish approved draft to relay:sends Redis Stream.
    Relay worker picks up, checks halt_service, enforces mailbox rate limits,
    dispatches via Instantly API, logs touch_sent to events.
    relay:sends stream not yet built — implement alongside Relay worker.
    """
    logger.warning(
        "ink.nodes: relay_dispatch STUB — company_id=%s draft_message_id=%s",
        state["company_id"], state.get("draft_message_id"),
    )
    return {"stage": InkStage.DONE}
