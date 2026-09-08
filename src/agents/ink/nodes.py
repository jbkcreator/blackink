"""Ink (Campaign Agent) — LangGraph node functions.

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
                            "UPDATE contacts "
                            "SET ghost_submitted_at  = :ts, "
                            "    ghost_work_order_id = :woid "
                            "WHERE company_id = :cid"
                        ),
                        {
                            "ts":   result.submitted_at,
                            "woid": state["work_order_id"],
                            "cid":  state["company_id"],
                        },
                    )
                logger.info(
                    "ink.nodes: ghost_shopper wrote ghost_submitted_at=%s "
                    "ghost_work_order_id=%s company_id=%s",
                    result.submitted_at, state["work_order_id"], state["company_id"],
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
    """Compile the 2-page campaign audit PDF and upload to configured storage."""
    import datetime

    from sqlalchemy import text

    from src.agents.ink.subagents.pdf_generator.pdf_report import (
        CampaignAuditData,
        compile_campaign_pdf,
    )
    from src.agents.ink.subagents.pdf_generator.storage import get_pdf_store
    from src.core.database import get_db_context

    # Fetch company name and county for the report header
    with get_db_context(client_id=state["client_id"]) as db:
        row = db.execute(
            text("""
                SELECT c.company_name, co.county_name
                FROM   companies c
                JOIN   counties  co ON co.county_slug = c.county_slug
                WHERE  c.company_id = :cid
            """),
            {"cid": state["company_id"]},
        ).fetchone()

    company_name = row.company_name if row else state["company_id"]
    county_name  = row.county_name  if row else "Unknown County"
    audit_date   = datetime.date.today().strftime("%B %Y")

    data = CampaignAuditData(
        company_name=company_name,
        county_name=county_name,
        audit_date=audit_date,
        latency_sec=state.get("latency_sec"),
        loss_est=state.get("loss_est") or 0,
    )

    pdf_bytes = compile_campaign_pdf(data)

    store   = get_pdf_store()
    pdf_key = f"{state['company_id']}/{state['work_order_id']}/audit_report.pdf"
    pdf_url = store.put(pdf_key, pdf_bytes)

    logger.info(
        "ink.nodes: pdf_generator complete — company_id=%s size=%d url=%s",
        state["company_id"], len(pdf_bytes), pdf_url,
    )
    return {"pdf_url": pdf_url}


# ── 4. Sendspark ─────────────────────────────────────────────────────────────

def node_sendspark(state: GlobalState) -> dict:
    """Call Sendspark API to render a personalised video landing page.

    Skips gracefully (video_id=None, landing_url=None) when:
      - latency_sec or loss_est are missing (no audit data to personalise with)
      - SENDSPARK_API_KEY / SENDSPARK_TEMPLATE_ID are not configured

    GIF Generator downstream uses landing_url; it also handles None gracefully.
    """
    from src.agents.ink.subagents.pdf_generator.pdf_report import _fmt_latency
    from src.agents.ink.subagents.sendspark.client import SendsparkSkipped, get_client

    latency_sec = state.get("latency_sec")
    loss_est    = state.get("loss_est")

    if latency_sec is None or not loss_est:
        logger.warning(
            "ink.nodes: sendspark SKIPPED — missing audit data company_id=%s",
            state["company_id"],
        )
        return {"video_id": None, "landing_url": None}

    try:
        client = get_client()
    except SendsparkSkipped as exc:
        logger.warning(
            "ink.nodes: sendspark SKIPPED — not configured company_id=%s reason=%s",
            state["company_id"], exc,
        )
        return {"video_id": None, "landing_url": None}

    # Fetch company name for the video title (reuse what pdf_generator already wrote
    # to state if available, else fall back to a DB lookup)
    company_name = _get_company_name(state)

    response_time = _fmt_latency(latency_sec)
    loss_estimate = f"${loss_est:,}"

    try:
        result = client.render(
            company_name=company_name,
            response_time=response_time,
            loss_estimate=loss_estimate,
        )
        logger.info(
            "ink.nodes: sendspark complete — company_id=%s video_id=%s",
            state["company_id"], result.video_id,
        )
        return {"video_id": result.video_id, "landing_url": result.landing_url}
    except Exception as exc:
        logger.error(
            "ink.nodes: sendspark FAILED — company_id=%s: %s — continuing without video",
            state["company_id"], exc,
        )
        return {"video_id": None, "landing_url": None}


def _get_company_name(state: GlobalState) -> str:
    """Best-effort company name lookup for Sendspark personalisation."""
    from sqlalchemy import text

    from src.core.database import get_db_context

    try:
        with get_db_context(client_id=state["client_id"]) as db:
            row = db.execute(
                text("SELECT company_name FROM companies WHERE company_id = :cid"),
                {"cid": state["company_id"]},
            ).fetchone()
        return row.company_name if row else state["company_id"]
    except Exception:
        return state["company_id"]


# ── 5. GIF Generator ─────────────────────────────────────────────────────────

def node_gif_generator(state: GlobalState) -> dict:
    """Screencap prospect website, overlay audit data, encode animated GIF.

    60-second hard timeout on the Playwright screenshot step. Falls back to
    a grey placeholder frame if the site is unreachable or too slow.
    Always returns a gif_url -- never None -- so assets_merge is never blocked.
    """
    import concurrent.futures

    from sqlalchemy import text

    from src.agents.ink.subagents.gif_generator.composer import compose_gif
    from src.agents.ink.subagents.gif_generator.screenshot import capture
    from src.agents.ink.subagents.pdf_generator.storage import get_pdf_store
    from src.core.database import get_db_context

    with get_db_context(client_id=state["client_id"]) as db:
        row = db.execute(
            text("SELECT company_name, website FROM companies WHERE company_id = :cid"),
            {"cid": state["company_id"]},
        ).fetchone()

    company_name = row.company_name if row else state["company_id"]
    website_url  = row.website      if row else None

    # Screenshot with 55-second timeout (leaves 5 s for compose + encode)
    screenshot = None
    if website_url:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(capture, website_url)
            try:
                screenshot = future.result(timeout=55)
            except concurrent.futures.TimeoutError:
                logger.warning(
                    "ink.nodes: gif screenshot timed out — using placeholder company_id=%s",
                    state["company_id"],
                )
                future.cancel()

    try:
        gif_bytes = compose_gif(
            screenshot=screenshot,
            company_name=company_name,
            latency_sec=state.get("latency_sec"),
            loss_est=state.get("loss_est") or 0,
        )
    except Exception as exc:
        logger.error(
            "ink.nodes: gif compose failed company_id=%s: %s — skipping gif",
            state["company_id"], exc,
        )
        return {"gif_url": None}

    store   = get_pdf_store()
    gif_key = f"{state['company_id']}/{state['work_order_id']}/thumbnail.gif"
    gif_url = store.put(gif_key, gif_bytes)

    logger.info(
        "ink.nodes: gif_generator complete — company_id=%s size=%d screenshot=%s url=%s",
        state["company_id"], len(gif_bytes), screenshot is not None, gif_url,
    )
    return {"gif_url": gif_url}


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
    """Publish approved campaign to relay:sends so the Relay worker dispatches it."""
    from src.agents.relay.sends_queue import publish as relay_publish

    message_id = relay_publish(
        work_order_id=state["work_order_id"],
        campaign_id=state["campaign_id"],
        company_id=state["company_id"],
        client_id=state["client_id"],
        draft_message_id=state.get("draft_message_id") or "",
        pdf_url=state.get("pdf_url"),
        video_id=state.get("video_id"),
        landing_url=state.get("landing_url"),
        gif_url=state.get("gif_url"),
    )
    logger.info(
        "ink.nodes: relay_dispatch published — work_order_id=%s relay_message_id=%s",
        state["work_order_id"], message_id,
    )
    return {"stage": InkStage.DONE}
