"""Ghost Shopper — nested LangGraph graph definition.

# DEFERRED 2026-09-03 — see runner.py for full context and reactivation notes.

Thread ID convention: "gs:{work_order_id}"
Checkpointed to the same Postgres instance as Ink, different namespace.

Loop topology:

  START → fetch_and_extract
                │ (route_after_fetch)
                ├─(blocked, no proxy)──► fetch_with_proxy ─┐
                └─(ok / proxy used)────────────────────────┤
                                                           ▼
                                                    form_validator
                                                        ├─(valid)──► fill_and_submit → confirm_check
                                                        │                                   ├─(SUBMITTED)──► END
                                                        │                                   └─(no confirm)─► queue_ranker
                                                        └─(no form)──────────────────────► queue_ranker
                                                                                                 │
                                                                                           loop_controller
                                                                                                 ├─(continue)──► fetch_and_extract
                                                                                                 └─(terminate)──► END
"""
from __future__ import annotations

import logging
from typing import Optional

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph

from src.agents.ink.subagents.ghost_shopper.nodes import (
    node_confirm_check,
    node_fetch_and_extract,
    node_fetch_with_proxy,
    node_fill_and_submit,
    node_form_validator,
    node_loop_controller,
    node_queue_ranker,
    route_after_confirm_check,
    route_after_fetch,
    route_after_form_validator,
    route_after_loop_controller,
)
from src.agents.ink.subagents.ghost_shopper.state import CrawlState

logger = logging.getLogger(__name__)


def build_graph(checkpointer: Optional[PostgresSaver] = None):
    """Build and compile the Ghost Shopper crawl graph.

    Pass the same PostgresSaver instance used by Ink — different thread_id
    namespace means no key collisions. Pass None for unit tests.
    """
    builder = StateGraph(CrawlState)

    # ── nodes ──────────────────────────────────────────────────────────────────
    builder.add_node("fetch_and_extract",  node_fetch_and_extract)
    builder.add_node("fetch_with_proxy",   node_fetch_with_proxy)
    builder.add_node("form_validator",     node_form_validator)
    builder.add_node("fill_and_submit",    node_fill_and_submit)
    builder.add_node("confirm_check",      node_confirm_check)
    builder.add_node("queue_ranker",       node_queue_ranker)
    builder.add_node("loop_controller",    node_loop_controller)

    # ── edges ───────────────────────────────────────────────────────────────────
    builder.add_edge(START, "fetch_and_extract")

    # If fetch was blocked (403/timeout/bot-wall) and proxy not yet tried,
    # route to proxy node; otherwise proceed directly to form_validator.
    builder.add_conditional_edges(
        "fetch_and_extract",
        route_after_fetch,
        {"fetch_with_proxy": "fetch_with_proxy", "form_validator": "form_validator"},
    )
    # After a proxy attempt always proceed to form_validator — one retry per URL.
    builder.add_edge("fetch_with_proxy", "form_validator")

    builder.add_conditional_edges(
        "form_validator",
        route_after_form_validator,
        {"fill_and_submit": "fill_and_submit", "queue_ranker": "queue_ranker"},
    )

    builder.add_edge("fill_and_submit", "confirm_check")

    builder.add_conditional_edges(
        "confirm_check",
        route_after_confirm_check,
        {"__end__": END, "queue_ranker": "queue_ranker"},
    )

    builder.add_edge("queue_ranker", "loop_controller")

    # Back-edge: loop_controller → fetch_and_extract creates the crawl loop
    builder.add_conditional_edges(
        "loop_controller",
        route_after_loop_controller,
        {"fetch_and_extract": "fetch_and_extract", "__end__": END},
    )

    graph = builder.compile(checkpointer=checkpointer)
    logger.info("ghost_shopper.graph: compiled — checkpointer=%s", type(checkpointer).__name__)
    return graph
