"""Ink (Campaign Agent) — global state and stage definitions.

GlobalState is the single source of truth for the entire campaign lifecycle.
It is owned by the LangGraph graph, persisted via Postgres checkpointer, and
survives process restarts across the two async suspend points (WAIT_REPLY,
WAIT_APPROVE).

Subagents (Ghost Shopper, PDF Generator, Sendspark, GIF Generator) maintain
their own local state internally and are never seen by GlobalState — only
their return values are written here.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Literal, Optional

from typing_extensions import TypedDict


class InkStage(str, Enum):
    GHOST_SHOPPER   = "ghost_shopper"
    WAIT_REPLY      = "wait_reply"       # suspend: waiting for IMAP reply
    PDF_GENERATOR   = "pdf_generator"
    SENDSPARK       = "sendspark"
    FEE_STACK       = "fee_stack"
    GIF_GENERATOR   = "gif_generator"
    ASSETS_MERGE    = "assets_merge"
    CORA_DISPATCH   = "cora_dispatch"
    WAIT_APPROVE    = "wait_approve"     # suspend: waiting for Slack approval
    RELAY_DISPATCH  = "relay_dispatch"
    DONE            = "done"
    FAILED          = "failed"


class GlobalState(TypedDict):
    # ── Identity ──────────────────────────────────────────────────────────────
    company_id:     str
    campaign_id:    str
    work_order_id:  str     # LangGraph thread_id — uniquely identifies this run
    client_id:      str

    # ── Lifecycle ─────────────────────────────────────────────────────────────
    stage:          InkStage

    # ── Ghost Shopper outputs ─────────────────────────────────────────────────
    ghost_result:   Optional[Literal["SUBMITTED", "FORM_NOT_FOUND", "ERROR"]]
    submitted_at:   Optional[int]           # ms epoch

    # ── IMAP reply (written by IMAP listener, consumed at WAIT_REPLY resume) ──
    latency_sec:    Optional[int]
    loss_est:       Optional[int]           # dollars, from revenue loss formula

    # ── Proof asset outputs ───────────────────────────────────────────────────
    pdf_url:          Optional[str]
    fee_stack_url:    Optional[str]         # Fee-Stack one-pager PDF
    video_id:         Optional[str]
    landing_url:      Optional[str]         # Sendspark personalised landing URL
    gif_url:          Optional[str]

    # ── Cora output ───────────────────────────────────────────────────────────
    draft_message_id: Optional[str]         # cora:drafts message_id

    # ── Error ─────────────────────────────────────────────────────────────────
    error:          Optional[str]


@dataclass
class WorkOrderMessage:
    """Parsed message from the ink:work_orders Redis Stream."""
    message_id:     str
    company_id:     str
    campaign_id:    str
    work_order_id:  str
    client_id:      str
    delivery_count: int = 1
