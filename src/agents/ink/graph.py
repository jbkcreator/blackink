"""Ink (Campaign Agent) — LangGraph graph definition.

# ============================================================
# DEFERRED — Ghost Shopper pipeline topology
# ============================================================
# Ghost Shopper, Sendspark, and GIF Generator stages were deferred on
# 2026-09-03 (Source of Truth). This graph topology is preserved for future
# reactivation.
#
# When reactivated, the active OVS pipeline (feature/owner-visibility-score-engine)
# must be merged in and the following changes made to this graph:
#   - Replace the ghost_shopper / wait_reply entry stages with an
#     ovs_score_lookup node that reads from owner_visibility_scores
#   - Remove the sendspark / gif_generator fan-out (or keep as deferred branches)
#   - Update GlobalState fields accordingly (see state.py deferred note)
# ============================================================

Topology (Ghost Shopper pipeline — deferred)
────────
START
  └─► ghost_shopper ──(SUBMITTED)──► wait_reply
                    ──(else)───────► END

wait_reply ──► [fan-out via Send]
  ├─► pdf_generator ─────────────────────────────────────► assets_merge
  └─► sendspark ──► gif_generator ──────────────────────► assets_merge

assets_merge ──► cora_dispatch ──► wait_approve
  ├─(approved)──► relay_dispatch ──► END
  └─(rejected)──► END

Both `wait_reply` and `wait_approve` call interrupt() — they suspend and
checkpoint to Postgres. Resume via:
    graph.invoke(Command(resume=data), config={"configurable": {"thread_id": work_order_id}})
"""
from __future__ import annotations

import logging
from typing import Optional

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.constants import Send
from langgraph.graph import END, START, StateGraph

from src.agents.ink.nodes import (
    make_node_ghost_shopper,
    node_assets_merge,
    node_cora_dispatch,
    node_gif_generator,
    node_pdf_generator,
    node_relay_dispatch,
    node_sendspark,
    node_wait_approve,
    node_wait_reply,
    route_after_approve,
    route_after_ghost,
)
from src.agents.ink.state import GlobalState

logger = logging.getLogger(__name__)


def _fan_out_to_assets(state: GlobalState) -> list:
    """After wait_reply resolves, launch pdf_generator and sendspark in parallel.

    Both branches converge at assets_merge. LangGraph guarantees assets_merge
    does not run until both pdf_generator and gif_generator are complete.
    gif_generator is downstream of sendspark (needs landing_url), so the
    convergence path is:
        pdf_generator  ─────────────────────────────────┐
        sendspark ──► gif_generator ────────────────────┤
                                                        └─► assets_merge
    """
    return [
        Send("pdf_generator", state),
        Send("sendspark",     state),
    ]


def build_graph(
    checkpointer: Optional[PostgresSaver] = None,
    ghost_shopper_graph=None,
) -> StateGraph:
    """Build and compile the Ink campaign graph.

    Args:
        checkpointer:        PostgresSaver for Ink's own suspend/resume.
        ghost_shopper_graph: Compiled Ghost Shopper nested graph. Built once
                             in worker.py and injected here via closure so both
                             graphs share the same Postgres checkpointer instance.
                             Pass None in tests (ghost_shopper node will error
                             if invoked, which is acceptable for graph-topology tests).
    """
    builder = StateGraph(GlobalState)

    # ── nodes ─────────────────────────────────────────────────────────────────
    builder.add_node("ghost_shopper",  make_node_ghost_shopper(ghost_shopper_graph))
    builder.add_node("wait_reply",     node_wait_reply)
    builder.add_node("pdf_generator",  node_pdf_generator)
    builder.add_node("sendspark",      node_sendspark)
    builder.add_node("gif_generator",  node_gif_generator)
    builder.add_node("assets_merge",   node_assets_merge)
    builder.add_node("cora_dispatch",  node_cora_dispatch)
    builder.add_node("wait_approve",   node_wait_approve)
    builder.add_node("relay_dispatch", node_relay_dispatch)

    # ── edges ──────────────────────────────────────────────────────────────────
    builder.add_edge(START, "ghost_shopper")

    builder.add_conditional_edges(
        "ghost_shopper",
        route_after_ghost,
        {"wait_reply": "wait_reply", "__end__": END},
    )

    # Fan-out: wait_reply → [pdf_generator, sendspark] in parallel
    builder.add_conditional_edges("wait_reply", _fan_out_to_assets)

    # Sequential: sendspark → gif_generator (gif needs landing_url)
    builder.add_edge("sendspark", "gif_generator")

    # Convergence: both pdf_generator and gif_generator → assets_merge
    builder.add_edge("pdf_generator",  "assets_merge")
    builder.add_edge("gif_generator",  "assets_merge")

    builder.add_edge("assets_merge",  "cora_dispatch")
    builder.add_edge("cora_dispatch", "wait_approve")

    builder.add_conditional_edges(
        "wait_approve",
        route_after_approve,
        {"relay_dispatch": "relay_dispatch", "__end__": END},
    )

    builder.add_edge("relay_dispatch", END)

    graph = builder.compile(checkpointer=checkpointer)
    logger.info("ink.graph: compiled — checkpointer=%s", type(checkpointer).__name__)
    return graph
