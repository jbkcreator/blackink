"""Ghost Shopper — unit and graph topology tests.

No Playwright, no LLM, no Postgres required.
Playwright calls are patched; LLM calls are patched.
Graph topology tests compile the graph with checkpointer=None and
verify node wiring without executing any node.

Run:
    pytest tests/test_ghost_shopper.py -v
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from src.agents.ink.subagents.ghost_shopper.state import CrawlState, SUBMISSION_TEMPLATE
from src.agents.ink.subagents.ghost_shopper.nodes import (
    _CONFIRM_TEXT_PATTERNS,
    _CONFIRM_URL_PATTERNS,
    node_confirm_check,
    node_form_validator,
    node_loop_controller,
    node_queue_ranker,
    route_after_confirm_check,
    route_after_form_validator,
    route_after_loop_controller,
)


# ── fixtures ──────────────────────────────────────────────────────────────────

def _base_state(**overrides) -> CrawlState:
    state: CrawlState = {
        "company_id":       "test-co-01",
        "work_order_id":    "wo-001",
        "start_url":        "https://example-pm.com",
        "current_url":      "https://example-pm.com",
        "visited":          [],
        "candidate_queue":  [],
        "current_forms":    [],
        "form_valid":       None,
        "depth":            0,
        "max_depth":        3,
        "max_llm_calls":    6,
        "llm_calls_used":   0,
        "submitted_at":     None,
        "post_submit_url":  None,
        "post_submit_body": None,
        "result":           "PENDING",
        "error":            None,
    }
    state.update(overrides)
    return state


# ── graph topology ─────────────────────────────────────────────────────────────

class TestGraphTopology:
    def test_graph_compiles(self):
        """Graph must compile without a checkpointer (no Postgres needed)."""
        from src.agents.ink.subagents.ghost_shopper.graph import build_graph
        graph = build_graph(checkpointer=None)
        assert graph is not None

    def test_all_nodes_present(self):
        from src.agents.ink.subagents.ghost_shopper.graph import build_graph
        graph = build_graph(checkpointer=None)
        node_names = set(graph.nodes.keys())
        expected = {
            "fetch_and_extract", "form_validator", "fill_and_submit",
            "confirm_check", "queue_ranker", "loop_controller",
            "__start__",
        }
        assert expected.issubset(node_names)


# ── routing functions ──────────────────────────────────────────────────────────

class TestRouting:
    def test_form_validator_valid_routes_to_fill(self):
        state = _base_state(form_valid=True)
        assert route_after_form_validator(state) == "fill_and_submit"

    def test_form_validator_invalid_routes_to_ranker(self):
        state = _base_state(form_valid=False)
        assert route_after_form_validator(state) == "queue_ranker"

    def test_confirm_check_submitted_routes_to_end(self):
        state = _base_state(result="SUBMITTED")
        assert route_after_confirm_check(state) == "__end__"

    def test_confirm_check_pending_routes_to_ranker(self):
        state = _base_state(result="PENDING")
        assert route_after_confirm_check(state) == "queue_ranker"

    def test_loop_controller_form_not_found_routes_to_end(self):
        state = _base_state(result="FORM_NOT_FOUND")
        assert route_after_loop_controller(state) == "__end__"

    def test_loop_controller_pending_routes_to_fetch(self):
        state = _base_state(result="PENDING")
        assert route_after_loop_controller(state) == "fetch_and_extract"


# ── node_form_validator ────────────────────────────────────────────────────────

class TestFormValidator:
    def test_no_forms_returns_invalid_without_llm(self):
        state = _base_state(current_forms=[])
        result = node_form_validator(state)
        assert result["form_valid"] is False
        # llm_calls_used should NOT increment when no forms present
        assert "llm_calls_used" not in result

    def test_valid_form_increments_llm_calls(self):
        forms = [{"action": "/contact", "method": "post", "fields": [
            {"type": "text", "name": "name", "placeholder": "Your Name", "label": "Name"},
            {"type": "email", "name": "email", "placeholder": "Email", "label": "Email"},
            {"type": "textarea", "name": "message", "placeholder": "Message", "label": "Message"},
        ]}]
        state = _base_state(current_forms=forms, llm_calls_used=2)
        llm_response = json.dumps({"valid": True, "confidence": 0.95, "reason": "owner inquiry form"})

        with patch(
            "src.agents.ink.subagents.ghost_shopper.nodes._call_llm",
            return_value=llm_response,
        ):
            result = node_form_validator(state)

        assert result["form_valid"] is True
        assert result["llm_calls_used"] == 3

    def test_invalid_form_classified_correctly(self):
        forms = [{"action": "/apply", "method": "post", "fields": [
            {"type": "text",   "name": "income",     "placeholder": "Monthly Income", "label": "Income"},
            {"type": "text",   "name": "employer",   "placeholder": "Employer",       "label": "Employer"},
            {"type": "text",   "name": "references", "placeholder": "References",     "label": "References"},
        ]}]
        state = _base_state(current_forms=forms)
        llm_response = json.dumps({"valid": False, "confidence": 0.99, "reason": "tenant rental application"})

        with patch(
            "src.agents.ink.subagents.ghost_shopper.nodes._call_llm",
            return_value=llm_response,
        ):
            result = node_form_validator(state)

        assert result["form_valid"] is False

    def test_llm_error_returns_invalid(self):
        forms = [{"action": "/contact", "method": "post", "fields": [
            {"type": "text", "name": "name", "placeholder": "", "label": "Name"},
        ]}]
        state = _base_state(current_forms=forms)

        with patch(
            "src.agents.ink.subagents.ghost_shopper.nodes._call_llm",
            side_effect=Exception("API error"),
        ):
            result = node_form_validator(state)

        assert result["form_valid"] is False


# ── node_confirm_check ────────────────────────────────────────────────────────

class TestConfirmCheck:
    def test_no_submitted_at_returns_pending(self):
        state = _base_state(submitted_at=None)
        result = node_confirm_check(state)
        assert result["result"] == "PENDING"

    def test_url_pattern_confirms_submission(self):
        for pattern in ["/thank-you", "/success", "/confirmation", "/submitted"]:
            state = _base_state(
                submitted_at=1700000000000,
                post_submit_url=f"https://example-pm.com{pattern}",
                post_submit_body="",
            )
            result = node_confirm_check(state)
            assert result["result"] == "SUBMITTED", f"pattern {pattern!r} should confirm"

    def test_body_text_confirms_submission(self):
        for phrase in ["thank you", "we'll be in touch", "message received", "request received"]:
            state = _base_state(
                submitted_at=1700000000000,
                post_submit_url="https://example-pm.com/contact",
                post_submit_body=f"some text {phrase} more text",
            )
            result = node_confirm_check(state)
            assert result["result"] == "SUBMITTED", f"phrase {phrase!r} should confirm"

    def test_ambiguous_post_submit_treated_as_submitted(self):
        """When submitted_at is set but no confirmation signal — treat as SUBMITTED."""
        state = _base_state(
            submitted_at=1700000000000,
            post_submit_url="https://example-pm.com/contact",  # no /thank pattern
            post_submit_body="welcome to our website please explore our services",
        )
        result = node_confirm_check(state)
        assert result["result"] == "SUBMITTED"


# ── node_loop_controller ──────────────────────────────────────────────────────

class TestLoopController:
    def test_empty_queue_terminates(self):
        state = _base_state(candidate_queue=[])
        result = node_loop_controller(state)
        assert result["result"] == "FORM_NOT_FOUND"

    def test_max_depth_terminates(self):
        state = _base_state(
            depth=3, max_depth=3,
            candidate_queue=[{"url": "https://x.com/contact", "score": 0.9, "depth": 4}],
        )
        result = node_loop_controller(state)
        assert result["result"] == "FORM_NOT_FOUND"

    def test_budget_exhausted_terminates(self):
        state = _base_state(
            llm_calls_used=6, max_llm_calls=6,
            candidate_queue=[{"url": "https://x.com/contact", "score": 0.9, "depth": 1}],
        )
        result = node_loop_controller(state)
        assert result["result"] == "FORM_NOT_FOUND"

    def test_pops_best_candidate_and_advances_depth(self):
        queue = [
            {"url": "https://x.com/contact", "score": 0.9, "depth": 1},
            {"url": "https://x.com/about",   "score": 0.4, "depth": 1},
        ]
        state = _base_state(candidate_queue=queue, depth=0)
        result = node_loop_controller(state)
        assert result["current_url"] == "https://x.com/contact"
        assert result["depth"] == 1
        assert len(result["candidate_queue"]) == 1
        assert result["candidate_queue"][0]["url"] == "https://x.com/about"

    def test_resets_per_iteration_fields(self):
        queue = [{"url": "https://x.com/contact", "score": 0.9, "depth": 1}]
        state = _base_state(candidate_queue=queue, current_forms=[{"some": "form"}], form_valid=True)
        result = node_loop_controller(state)
        assert result["current_forms"] == []
        assert result["form_valid"] is None
        assert result["post_submit_url"] is None
        assert result["post_submit_body"] is None


# ── node_queue_ranker ─────────────────────────────────────────────────────────

class TestQueueRanker:
    def test_empty_queue_returns_empty(self):
        state = _base_state(candidate_queue=[])
        result = node_queue_ranker(state)
        assert result == {}

    def test_budget_exhausted_sorts_by_existing_score(self):
        queue = [
            {"url": "https://x.com/blog",    "score": 0.1, "depth": 1},
            {"url": "https://x.com/contact", "score": 0.9, "depth": 1},
        ]
        state = _base_state(candidate_queue=queue, llm_calls_used=6, max_llm_calls=6)
        result = node_queue_ranker(state)
        assert result["candidate_queue"][0]["url"] == "https://x.com/contact"

    def test_llm_scores_applied_and_sorted(self):
        queue = [
            {"url": "https://x.com/blog",    "score": 0.5, "depth": 1},
            {"url": "https://x.com/contact", "score": 0.5, "depth": 1},
        ]
        state = _base_state(candidate_queue=queue, llm_calls_used=1, max_llm_calls=6)
        llm_response = json.dumps([
            {"url": "https://x.com/contact", "score": 0.95},
            {"url": "https://x.com/blog",    "score": 0.05},
        ])
        with patch(
            "src.agents.ink.subagents.ghost_shopper.nodes._call_llm",
            return_value=llm_response,
        ):
            result = node_queue_ranker(state)

        assert result["candidate_queue"][0]["url"] == "https://x.com/contact"
        assert result["candidate_queue"][0]["score"] == 0.95
        assert result["llm_calls_used"] == 2

    def test_llm_error_returns_empty_dict(self):
        queue = [{"url": "https://x.com/contact", "score": 0.5, "depth": 1}]
        state = _base_state(candidate_queue=queue)
        with patch(
            "src.agents.ink.subagents.ghost_shopper.nodes._call_llm",
            side_effect=Exception("timeout"),
        ):
            result = node_queue_ranker(state)
        assert result == {}


# ── submission template ───────────────────────────────────────────────────────

class TestSubmissionTemplate:
    def test_has_all_required_fields(self):
        for field in ("name", "email", "phone", "address", "message"):
            assert field in SUBMISSION_TEMPLATE
            assert SUBMISSION_TEMPLATE[field]

    def test_email_is_set(self):
        assert "@" in SUBMISSION_TEMPLATE["email"]
        assert SUBMISSION_TEMPLATE["email"]
