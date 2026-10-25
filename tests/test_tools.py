from pathlib import Path

import pytest

from app.loader import load_bank, load_ledger
from app.reconciliation import reconcile
from app.service import ReconciliationService
from app.tools import TOOL_SCHEMAS, ToolInputError, ToolRegistry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def registry():
    return ToolRegistry(ReconciliationService(reconcile(load_ledger(ROOT / "data/ledger.csv"), load_bank(ROOT / "data/bank.csv"))))


def test_exactly_three_strict_read_only_schemas():
    assert {t["name"] for t in TOOL_SCHEMAS} == {"get_summary", "list_exceptions", "get_payout"}
    for tool in TOOL_SCHEMAS:
        assert tool["strict"] is True
        assert tool["parameters"]["additionalProperties"] is False
        assert set(tool["parameters"]["required"]) == set(tool["parameters"]["properties"])


def test_tools_return_authoritative_service_data(registry):
    summary, execution = registry.execute("get_summary", "{}", "request-1", "demo-mode")
    assert summary == {"total_payouts": 12, "matched": 7, "exceptions": 5, "unmatched_bank": 1}
    assert execution.name == "get_summary" and execution.outcome == "success"
    exceptions, _ = registry.execute("list_exceptions", "{}", "request-1", "demo-mode")
    assert len(exceptions["payout_exceptions"]) == 5
    assert [b["bank_txn_id"] for b in exceptions["unmatched_bank"]] == ["B-999"]
    payout, _ = registry.execute("get_payout", '{"payout_id":"P-003"}', "request-1", "demo-mode")
    assert payout["status"] == "AMOUNT_MISMATCH"
    assert payout["candidate_bank_txn_ids"] == ["B-003"]
    assert payout["evidence"]["rule_id"] == "R5_AMOUNT_MISMATCH"
    assert payout["evidence"]["amount_difference_cents"] == -50


@pytest.mark.parametrize("name,args", [
    ("execute_refund", '{"payout_id":"P-003"}'), ("__dict__", "{}"),
    ("get_summary", '{"extra":1}'), ("list_exceptions", '{"question":"secret"}'),
    ("get_payout", '{}'), ("get_payout", '{"payout_id":3}'),
    ("get_payout", '{"payout_id":"../../etc/passwd"}'),
    ("get_payout", '{"payout_id":"P-003","refund":true}'),
    ("get_payout", '{"payout_id":"P-003","payout_id":"P-005"}'),
    ("get_payout", '{"payout_id":""}'), ("get_summary", "null"),
    ("get_summary", "[]"), ("get_summary", "not-json"),
    ("get_summary", '{"extra":NaN}'), ("get_summary", " " * 4097),
])
def test_tool_boundary_rejects_unregistered_or_malformed_requests(registry, name, args):
    with pytest.raises(ToolInputError):
        registry.execute(name, args, "request-1", "live-llm")


def test_unknown_payout_error_is_explicit(registry):
    with pytest.raises(ToolInputError) as error:
        registry.execute("get_payout", '{"payout_id":"P-404"}', "request-1", "demo-mode")
    assert error.value.code == "payout_not_found"


def test_tool_logs_only_metadata(registry, caplog):
    caplog.set_level("INFO", logger="payment_recon")
    registry.execute("get_payout", '{"payout_id":"P-003"}', "request-1", "demo-mode")
    record = next(r for r in caplog.records if r.getMessage() == "tool_execution")
    assert record.request_id == "request-1"
    assert record.mode == "demo-mode" and record.tool_name == "get_payout"
    assert record.outcome == "success" and record.duration_ms >= 0
    assert "arguments" not in record.__dict__ and "result" not in record.__dict__
