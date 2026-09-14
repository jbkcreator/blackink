"""Tests: node_fee_stack emits fee_stack_pdf_generated via log_event on success."""
import sys
from types import ModuleType
from unittest.mock import MagicMock, patch

# Stub out heavy/unavailable deps before any import resolves them.
def _stub_module(name, **attrs):
    m = ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules[name] = m
    return m

if "fpdf" not in sys.modules:
    _stub_module("fpdf", FPDF=MagicMock)
if "src.agents.ink.subagents.fee_stack.fee_stack_report" not in sys.modules:
    _stub_module(
        "src.agents.ink.subagents.fee_stack.fee_stack_report",
        FeeStackData=MagicMock,
        compile_fee_stack_pdf=MagicMock(return_value=b"%PDF-1.4"),
    )
if "src.agents.ink.subagents.pdf_generator.storage" not in sys.modules:
    _stub_module(
        "src.agents.ink.subagents.pdf_generator.storage",
        get_pdf_store=MagicMock(),
    )


def _state(company_id="co-1", work_order_id="wo-1", client_id="cl-1"):
    return {
        "company_id": company_id,
        "work_order_id": work_order_id,
        "client_id": client_id,
        "campaign_id": "cam-1",
    }


class TestNodeFeeStackEvent:
    def test_emits_fee_stack_pdf_generated_on_success(self):
        fake_row = MagicMock()
        fake_row.company_name = "Acme PM"
        fake_row.county_name = "Hillsborough"
        fake_row.door_count_est = 50

        fake_session = MagicMock()
        fake_session.execute.return_value.fetchone.return_value = fake_row
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_session)
        ctx.__exit__ = MagicMock(return_value=False)

        fake_store = MagicMock()
        fake_store.put.return_value = "https://storage.example.com/fee_stack.pdf"

        # log_event is a module-level import in nodes.py — patch it there.
        with patch("src.core.database.get_db_context", return_value=ctx), \
             patch("src.agents.ink.subagents.fee_stack.fee_stack_report.compile_fee_stack_pdf",
                   return_value=b"%PDF-1.4"), \
             patch("src.agents.ink.subagents.pdf_generator.storage.get_pdf_store",
                   return_value=fake_store), \
             patch("src.services.events.log_event") as mock_log_event:
            from src.agents.ink.nodes import node_fee_stack
            result = node_fee_stack(_state())

        assert result["fee_stack_url"] == "https://storage.example.com/fee_stack.pdf"
        mock_log_event.assert_called_once()
        call = mock_log_event.call_args
        # Accepts both positional and keyword call styles.
        event_type = call.kwargs.get("event_type") or (call.args[1] if len(call.args) > 1 else "")
        assert event_type == "fee_stack_pdf_generated"
        payload = call.kwargs.get("payload", {})
        assert "company_id" in payload
        assert "fee_stack_url" in payload
        assert payload["fee_stack_url"] == "https://storage.example.com/fee_stack.pdf"

    def test_does_not_emit_success_event_on_store_failure(self):
        fake_row = MagicMock()
        fake_row.company_name = "Acme PM"
        fake_row.county_name = "Hillsborough"
        fake_row.door_count_est = 50

        fake_session = MagicMock()
        fake_session.execute.return_value.fetchone.return_value = fake_row
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=fake_session)
        ctx.__exit__ = MagicMock(return_value=False)

        with patch("src.core.database.get_db_context", return_value=ctx), \
             patch("src.agents.ink.subagents.fee_stack.fee_stack_report.compile_fee_stack_pdf",
                   return_value=b"%PDF-1.4"), \
             patch("src.agents.ink.subagents.pdf_generator.storage.get_pdf_store",
                   side_effect=RuntimeError("storage down")), \
             patch("src.services.events.log_event") as mock_log_event:
            from src.agents.ink.nodes import node_fee_stack
            result = node_fee_stack(_state())

        assert result["fee_stack_url"] is None
        # Only the failure event fires — never the success event.
        for call in mock_log_event.call_args_list:
            event_type = call.kwargs.get("event_type") or (call.args[1] if len(call.args) > 1 else "")
            assert event_type != "fee_stack_pdf_generated"
