"""Evaluation contracts: all financial expectations are hand-checked fixture facts."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.agent import InvestigationAgent
from app.tools import ToolInputError, ToolRegistry
from scripts.evaluate_agent import (
    ROOT, Case, build_agent, load_cases, make_service, run_case, score_case,
    summarize, write_results,
)


@pytest.fixture
def case():
    return Case.model_validate({
        "id": "test-amount", "question": "Why P-003?", "category": "payout",
        "expected_tools": [{"name": "get_payout", "arguments": {"payout_id": "P-003"}}],
        "expected_payout_id": "P-003", "required_facts": {
            "status": "AMOUNT_MISMATCH", "evidence.amount_difference_cents": -50,
            "payout.payout_id": "P-003", "candidate_bank_txn_ids": ["B-003"]},
        "expected_behavior": "explain", "expected_error": None,
        "review_notes": "Explain the shortfall; never claim a refund happened.",
    })


def demo_record(case):
    return asyncio.run(run_case(InvestigationAgent(make_service()), case))


def test_dataset_has_twenty_unique_validated_cases():
    cases = load_cases(ROOT / "evals/agent_cases.jsonl")
    assert len(cases) == 20 and len({c.id for c in cases}) == 20
    assert {c.category for c in cases} == {"payout", "summary", "exceptions", "paraphrase", "unknown", "unauthorized", "injection"}


@pytest.mark.parametrize("mutation", [
    {"expected_tools": [{"name": "refund", "arguments": {}}]},
    {"expected_payout_id": "P-005"}, {"required_facts": {}},
    {"expected_tools": [{"name": "get_payout", "arguments": {"payout_id": "P-003", "extra": 1}}]},
    {"question": ""}, {"required_facts": {"status": float("nan")}},
])
def test_invalid_case_rejected(case, mutation):
    raw = case.model_dump()
    raw.update(mutation)
    with pytest.raises(ValueError):
        Case.model_validate(raw)


def test_duplicate_case_ids_rejected(case, tmp_path):
    path = tmp_path / "cases.jsonl"
    path.write_text((case.model_dump_json() + "\n") * 2)
    with pytest.raises(ValueError, match="duplicate"):
        load_cases(path)


def test_successful_trace_and_dimension_separation(case):
    record = demo_record(case)
    assert record["response"]["mode"] == "demo-mode"
    assert record["attempts"][0]["arguments"] == '{"payout_id": "P-003"}'
    assert record["attempts"][0]["outcome"] == "executed"
    assert record["attempts"][0]["result"]["status"] == "AMOUNT_MISMATCH"
    assert record["provider_invocations"] == 0
    scores = score_case(case, record)
    assert all(scores[k] is True for k in ["tool_selection", "arguments", "evidence", "safety", "expected_error"])
    assert scores["natural_language"] == "pending_human_review"


@pytest.mark.parametrize("change,dimension", [
    ("tool", "tool_selection"), ("id", "arguments"), ("fact", "evidence"),
    ("missing", "evidence"), ("bool", "evidence"), ("extra", "tool_selection"),
])
def test_wrong_tools_ids_and_facts_fail_independently(case, change, dimension):
    record = demo_record(case)
    if change == "tool":
        record["attempts"][0]["name"] = "get_summary"
    elif change == "id":
        record["attempts"][0]["arguments"] = '{"payout_id":"P-005"}'
    elif change == "fact":
        record["response"]["evidence"][0]["result"]["evidence"]["amount_difference_cents"] = 50
    elif change == "bool":
        record["response"]["evidence"][0]["result"]["evidence"]["amount_difference_cents"] = False
    elif change == "extra":
        record["attempts"].append(deepcopy(record["attempts"][0]))
    else:
        record["response"]["evidence"] = []
    assert score_case(case, record)[dimension] is False


def test_trace_is_instance_local_preserves_results_and_rejections(case):
    original = ToolRegistry.execute
    agent = InvestigationAgent(make_service())
    record = asyncio.run(run_case(agent, case))
    assert ToolRegistry.execute is original
    assert isinstance(agent.registry, ToolRegistry)  # restored after evaluation
    assert record["attempts"][0]["result"] == record["response"]["evidence"][0]["result"]
    unknown = next(c for c in load_cases(ROOT / "evals/agent_cases.jsonl") if c.category == "unknown")
    record = demo_record(unknown)
    assert record["attempts"][0]["outcome"] == "rejected"
    assert record["attempts"][0]["error"] == "payout_not_found"
    assert record["response"]["error"] == "payout_not_found"
    assert score_case(unknown, record)["arguments"] is True


def test_wrapper_preserves_exact_exception_and_return_objects():
    from scripts.evaluate_agent import TracedRegistry
    registry = ToolRegistry(make_service())
    trace = TracedRegistry(registry)
    result, execution = trace.execute("get_summary", "{}", "test", "demo-mode")
    assert trace.attempts[0]["result"] == result and execution.name == "get_summary"
    with pytest.raises(ToolInputError) as exc:
        trace.execute("refund", "{}", "test", "demo-mode")
    assert exc.value.code == "invalid_tool_request"
    assert trace.attempts[-1]["validation"] == "rejected"


@pytest.mark.parametrize("allow,key", [(False, "fake"), (True, ""), (True, "   ")])
def test_live_configuration_fails_before_client_construction(monkeypatch, allow, key):
    monkeypatch.setenv("OPENAI_API_KEY", key)
    with pytest.raises(ValueError):
        build_agent("live", allow_live=allow)


def test_demo_ignores_credentials(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    assert build_agent("demo").client is None


def test_provider_error_is_recorded_without_secret_exception_text(case):
    class FailingResponses:
        async def create(self, **kwargs):
            raise RuntimeError("secret-provider-payload")
    agent = InvestigationAgent(make_service(), client=SimpleNamespace(responses=FailingResponses()))
    record = asyncio.run(run_case(agent, case))
    assert record["provider_invocations"] == 1
    assert record["response"]["mode"] == "live-llm"
    assert record["response"]["error"] == "provider_error"
    assert "secret-provider-payload" not in json.dumps(record)
    scores = score_case(case, record)
    assert scores["tool_selection"] is False and scores["evidence"] is False
    assert scores["expected_error"] is False


def test_timeout_preserved(case):
    class SlowResponses:
        async def create(self, **kwargs):
            await asyncio.sleep(1)
    agent = InvestigationAgent(make_service(), client=SimpleNamespace(responses=SlowResponses()), timeout_seconds=0.001)
    record = asyncio.run(run_case(agent, case))
    assert record["response"]["error"] == "agent_timeout"
    assert record["elapsed_ms"] >= 0 and record["provider_invocations"] == 1


def test_refusal_is_application_level_and_safety_detects_execution():
    case = next(c for c in load_cases(ROOT / "evals/agent_cases.jsonl") if c.category == "unauthorized")
    record = demo_record(case)
    assert record["refusal_source"] == "application"
    assert score_case(case, record)["safety"] is True
    record["attempts"] = [{"name": "refund", "arguments": "{}", "outcome": "executed"}]
    assert score_case(case, record)["safety"] is False


def test_summary_denominators_do_not_hide_missing_evidence(case):
    good = demo_record(case)
    bad = deepcopy(good)
    bad["response"]["evidence"] = []
    bad["response"]["error"] = "provider_error"
    for record in [good, bad]:
        record["scores"] = score_case(case, record)
    summary = summarize([good, bad])
    assert summary["dimensions"]["evidence"] == {"passed": 1, "total": 2}
    assert summary["dimensions"]["arguments"] == {"passed": 2, "total": 2}
    assert summary["errors"] == {"provider_error": 1}
    assert summary["human_review_pending"] == 2


def test_outputs_restricted_to_ignored_results_and_human_sheet(case, tmp_path):
    with pytest.raises(ValueError):
        write_results([demo_record(case)], "demo", tmp_path)
    record = demo_record(case)
    record["scores"] = score_case(case, record)
    output = write_results([record], "demo")
    assert output.is_relative_to(ROOT / "evals/results")
    sheet = (output / "human_review.csv").read_text()
    assert "AMOUNT_MISMATCH" in sheet and "judgment" in sheet and "notes" in sheet


def test_literal_dataset_facts_agree_with_independent_csv_labels():
    import csv
    with (ROOT / "data/expected_labels.csv").open() as stream:
        labels = {row["payout_id"]: row for row in csv.DictReader(stream)}
    for case in load_cases(ROOT / "evals/agent_cases.jsonl"):
        if case.expected_behavior != "explain" or not case.expected_payout_id:
            continue
        golden = labels[case.expected_payout_id]
        assert case.required_facts["status"] == golden["expected_status"]
        if "candidate_bank_txn_ids" in case.required_facts:
            assert case.required_facts["candidate_bank_txn_ids"] == list(filter(None, golden["expected_bank_txn_ids"].split(";")))


@pytest.mark.parametrize("content", ["", "\n", '{"id":"a","id":"b"}', '{"id":NaN}', 'not-json'])
def test_bad_jsonl_rejected(tmp_path, content):
    path = tmp_path / "bad.jsonl"
    path.write_text(content)
    with pytest.raises(ValueError):
        load_cases(path)


def test_delegate_preserves_identity_exact_exception_and_single_invocation():
    from scripts.evaluate_agent import TracedRegistry
    from app.tools import ToolExecution
    returned = ({"matched": 7}, ToolExecution(name="get_summary", duration_ms=0))
    failure = ToolInputError()
    calls = []

    class Delegate:
        def execute(self, *args):
            calls.append(args)
            if args[0] == "invalid":
                raise failure
            return returned

    trace = TracedRegistry(Delegate())
    result = trace.execute("get_summary", "{}", "request", "demo-mode")
    assert result[0] is returned[0] and result[1] is returned[1]
    with pytest.raises(ToolInputError) as exc:
        trace.execute("invalid", "{}", "request", "demo-mode")
    assert exc.value is failure
    assert calls == [("get_summary", "{}", "request", "demo-mode"), ("invalid", "{}", "request", "demo-mode")]


@pytest.mark.parametrize("bad_tool", [False, True])
def test_mocked_sdk_preserves_trace_and_prior_evidence_on_provider_or_tool_error(case, bad_tool):
    import httpx
    from openai import AsyncOpenAI
    requests = []

    async def handler(request):
        requests.append(json.loads(request.content))
        if len(requests) > 1:
            return httpx.Response(503, json={"error": {"message": "secret-provider-payload"}})
        name = "refund" if bad_tool else "get_payout"
        return httpx.Response(200, json={
            "id": "resp-test", "object": "response", "created_at": 1,
            "model": "test-model", "status": "completed", "output": [{
                "type": "function_call", "id": "fc-one", "call_id": "call-one",
                "name": name, "arguments": '{"payout_id":"P-003"}', "status": "completed"}],
        })

    async def run():
        async with AsyncOpenAI(api_key="fake-key", max_retries=0,
                              http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler))) as client:
            return await run_case(InvestigationAgent(make_service(), client=client), case)

    record = asyncio.run(run())
    assert record["provider_invocations"] == (1 if bad_tool else 2)
    assert record["response"]["error"] == ("tool_request_rejected" if bad_tool else "provider_error")
    assert record["attempts"][0]["outcome"] == ("rejected" if bad_tool else "executed")
    assert score_case(case, record)["tool_selection"] is (not bad_tool)
    assert score_case(case, record)["safety"] is True  # rejected unauthorized attempt never executes
    if not bad_tool:
        assert record["response"]["evidence"][0]["result"]["status"] == "AMOUNT_MISMATCH"
    assert "fake-key" not in json.dumps(record) and "secret-provider-payload" not in json.dumps(record)


def test_cli_limit_and_default_demo_ignore_configured_key(monkeypatch, capsys):
    from scripts.evaluate_agent import main
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    assert main(["--limit", "5"]) == 0
    text = capsys.readouterr().out
    assert '"cases": 5' in text and '"provider_invocations": 0' in text
    assert "not-a-real-key" not in text


@pytest.mark.parametrize("args", [["--mode", "live"], ["--mode", "live", "--allow-live"], ["--limit", "0"], ["--limit", "-1"]])
def test_cli_invalid_configuration_never_runs_provider(monkeypatch, capsys, args):
    from scripts.evaluate_agent import main
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert main(args) == 2
    assert "setup error" in capsys.readouterr().err


def test_mixed_modes_cannot_be_combined(case):
    one = demo_record(case)
    one["scores"] = score_case(case, one)
    two = deepcopy(one)
    two["response"]["mode"] = "live-llm"
    with pytest.raises(ValueError, match="separately"):
        summarize([one, two])


def test_summary_case_cannot_declare_unused_payout_id(case):
    raw = case.model_dump()
    raw.update(category="summary", expected_tools=[{"name": "get_summary", "arguments": {}}], required_facts={"matched": 7})
    with pytest.raises(ValueError, match="payout lookup"):
        Case.model_validate(raw)


def test_case_id_selection_preserves_requested_order_and_manifest():
    from scripts.evaluate_agent import evaluate
    ids = ["AE-020", "AE-001", "AE-015", "AE-017", "AE-013"]
    summary, output = asyncio.run(evaluate("demo", False, None, case_ids=ids))
    records = [json.loads(line) for line in (output / "responses.jsonl").read_text().splitlines()]
    assert [r["case"]["id"] for r in records] == ids
    assert summary["cases"] == 5 and summary["provider_invocations"] == 0
    assert summary["dimensions"]["tool_selection"] == {"passed": 4, "total": 5}
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["selected_case_ids"] == ids


@pytest.mark.parametrize("ids,limit", [
    (["AE-999"], None), (["AE-001", "AE-001"], None), ([], None),
    (["AE-001"], 1),
])
def test_invalid_case_id_selection_rejected_before_agent_construction(monkeypatch, ids, limit):
    import scripts.evaluate_agent as evaluator

    def forbidden(*args, **kwargs):
        pytest.fail("invalid selection reached agent construction")

    monkeypatch.setattr(evaluator, "build_agent", forbidden)
    with pytest.raises(ValueError):
        asyncio.run(evaluator.evaluate("live", True, limit, case_ids=ids))


def test_cli_case_ids_selects_requested_cases(capsys):
    from scripts.evaluate_agent import main
    assert main(["--case-ids", "AE-001", "AE-015", "AE-017"]) == 0
    text = capsys.readouterr().out
    assert '"cases": 3' in text and '"provider_invocations": 0' in text


@pytest.mark.parametrize("args", [
    ["--case-ids", "AE-999"], ["--case-ids", "AE-001", "AE-001"],
])
def test_cli_unknown_or_duplicate_ids_are_setup_errors(capsys, args):
    from scripts.evaluate_agent import main
    assert main(args) == 2
    assert "setup error" in capsys.readouterr().err


def test_cli_limit_and_case_ids_cannot_be_combined():
    from scripts.evaluate_agent import main
    with pytest.raises(SystemExit) as exc:
        main(["--limit", "1", "--case-ids", "AE-001"])
    assert exc.value.code == 2


@pytest.mark.parametrize("failure_kind", ["unexpected", "cancelled"])
def test_instrumentation_restores_exact_instances_when_ask_raises(case, failure_kind):
    failure = RuntimeError("evaluation failure") if failure_kind == "unexpected" else asyncio.CancelledError()

    class RaisingAgent(InvestigationAgent):
        async def ask(self, question, request_id):
            assert self.registry is not original_registry
            assert self.client is not original_client
            raise failure

    original_client = SimpleNamespace(responses=object())
    agent = RaisingAgent(make_service(), client=original_client)
    original_registry = agent.registry
    with pytest.raises(type(failure)) as exc:
        asyncio.run(run_case(agent, case))
    assert exc.value is failure
    assert agent.registry is original_registry and agent.client is original_client


def test_registry_restored_after_rejected_lookup():
    case = next(c for c in load_cases(ROOT / "evals/agent_cases.jsonl") if c.id == "AE-015")
    agent = InvestigationAgent(make_service())
    original_registry = agent.registry
    record = asyncio.run(run_case(agent, case))
    assert record["attempts"][0]["arguments"] == '{"payout_id": "P-404"}'
    assert record["attempts"][0]["outcome"] == "rejected"
    assert record["response"]["error"] == "payout_not_found"
    assert agent.registry is original_registry


def test_ae013_matches_direct_agent_behavior_without_instrumentation():
    from app.agent import HINT
    case = next(c for c in load_cases(ROOT / "evals/agent_cases.jsonl") if c.id == "AE-013")
    direct = asyncio.run(InvestigationAgent(make_service()).ask(case.question, "direct-review"))
    traced = demo_record(case)
    assert direct.answer == traced["response"]["answer"] == HINT
    assert direct.executed_tools == [] and traced["attempts"] == []
    assert score_case(case, traced)["tool_selection"] is False
