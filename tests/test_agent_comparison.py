"""Paired comparison tests use demo execution and synthetic saved live records only."""
import asyncio
from copy import deepcopy
import json

import pytest

from app.agent import InvestigationAgent
from scripts.evaluate_agent import ROOT, load_cases, make_service, run_case
from scripts.compare_agent_evaluations import compare_runs


@pytest.fixture
def saved_pair(tmp_path):
    case = load_cases(ROOT / "evals/agent_cases.jsonl")[0]
    record = asyncio.run(run_case(InvestigationAgent(make_service()), case))
    # The scorer must recompute, not trust these deliberately incorrect stored scores.
    record["scores"] = {"tool_selection": False}
    hashes = {name: "a" * 64 for name in ["evals/agent_cases.jsonl", "data/ledger.csv", "data/bank.csv", "data/expected_labels.csv"]}
    for name, mode in [("baseline", "demo"), ("live", "live")]:
        path = tmp_path / name
        path.mkdir()
        row = deepcopy(record)
        row["response"]["mode"] = "demo-mode" if mode == "demo" else "live-llm"
        row["provider_invocations"] = 0 if mode == "demo" else 2
        (path / "responses.jsonl").write_text(json.dumps(row) + "\n")
        (path / "manifest.json").write_text(json.dumps({"mode": mode, "model": None if mode == "demo" else "synthetic-test-model", "sha256": hashes}))
    return tmp_path / "baseline", tmp_path / "live"


def change_record(path, transform):
    file = path / "responses.jsonl"
    row = json.loads(file.read_text())
    transform(row)
    file.write_text(json.dumps(row) + "\n")


def test_missing_live_is_explicitly_unevaluated(saved_pair):
    baseline, _ = saved_pair
    result = compare_runs(baseline)
    assert result["cases"] == ["AE-001"]
    assert result["baseline"]["dimensions"]["tool_selection"] == {"passed": 1, "total": 1}
    assert result["live"] == "Not yet evaluated"
    assert result["baseline"]["narrative_review"] == {"status": "Pending", "eligible": 1}


def test_pair_reuses_scores_and_keeps_modes_and_narratives_separate(saved_pair):
    baseline, live = saved_pair
    result = compare_runs(baseline, live)
    assert result["live"]["dimensions"]["evidence"] == {"passed": 1, "total": 1}
    assert result["baseline"]["provider_invocations"] == 0
    assert result["live"]["provider_invocations"] == 2
    assert result["live"]["model"] == "synthetic-test-model"
    assert result["live"]["narrative_review"]["status"] == "Pending"


@pytest.mark.parametrize("mutation,dimension", [("tool", "tool_selection"), ("argument", "arguments"), ("fact", "evidence")])
def test_comparison_distinguishes_tool_argument_and_fact_failures(saved_pair, mutation, dimension):
    baseline, live = saved_pair

    def mutate(row):
        if mutation == "tool":
            row["attempts"][0]["name"] = "get_summary"
        elif mutation == "argument":
            row["attempts"][0]["arguments"] = '{"payout_id":"P-005"}'
        else:
            row["response"]["evidence"][0]["result"]["evidence"]["amount_difference_cents"] = 50
    change_record(live, mutate)
    result = compare_runs(baseline, live)
    assert result["live"]["dimensions"][dimension] == {"passed": 0, "total": 1}
    assert result["baseline"]["dimensions"][dimension] == {"passed": 1, "total": 1}
    for other in {"tool_selection", "arguments", "evidence"} - {dimension}:
        assert result["live"]["dimensions"][other] == {"passed": 1, "total": 1}


def test_provider_failure_is_separate_from_retained_evidence(saved_pair):
    baseline, live = saved_pair
    change_record(live, lambda row: row["response"].update(error="provider_error", answer="Provider unavailable."))
    result = compare_runs(baseline, live)["live"]
    assert result["errors"] == {"provider_error": 1}
    assert result["dimensions"]["evidence"] == {"passed": 1, "total": 1}
    assert result["dimensions"]["expected_error"] == {"passed": 0, "total": 1}
    assert result["narrative_review"]["status"] == "Pending"


@pytest.mark.parametrize("mutation", ["question", "facts", "missing_case", "duplicate_case", "mode", "hash", "missing_manifest", "latency"])
def test_unpaired_or_invalid_saved_runs_rejected(saved_pair, mutation):
    baseline, live = saved_pair
    if mutation == "question":
        change_record(live, lambda row: row["case"].update(question="Different question"))
    elif mutation == "facts":
        change_record(live, lambda row: row["case"]["required_facts"].update({"evidence.amount_difference_cents": 50}))
    elif mutation == "missing_case":
        (live / "responses.jsonl").write_text("")
    elif mutation == "duplicate_case":
        file = live / "responses.jsonl"
        file.write_text(file.read_text() * 2)
    elif mutation == "mode":
        change_record(live, lambda row: row["response"].update(mode="demo-mode"))
    elif mutation == "hash":
        file = live / "manifest.json"
        manifest = json.loads(file.read_text())
        manifest["sha256"]["data/ledger.csv"] = "b" * 64
        file.write_text(json.dumps(manifest))
    elif mutation == "missing_manifest":
        (live / "manifest.json").unlink()
    else:
        change_record(live, lambda row: row.update(elapsed_ms=-1))
    with pytest.raises((ValueError, OSError)):
        compare_runs(baseline, live)


def test_selected_case_sets_must_match(saved_pair):
    baseline, live = saved_pair
    extra = json.loads((live / "responses.jsonl").read_text())
    extra["case"]["id"] = "extra-case"
    with (live / "responses.jsonl").open("a") as stream:
        stream.write(json.dumps(extra) + "\n")
    with pytest.raises(ValueError, match="case IDs"):
        compare_runs(baseline, live)


def test_cli_emits_comparison_and_never_constructs_agent(saved_pair, capsys):
    from scripts.compare_agent_evaluations import main
    assert main(["--baseline", str(saved_pair[0])]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["live"] == "Not yet evaluated"


def test_rejected_attempts_and_application_refusals_are_distinct(saved_pair):
    baseline, live = saved_pair
    cases = [c for c in load_cases(ROOT / "evals/agent_cases.jsonl") if c.id in {"AE-015", "AE-017"}]
    records = [asyncio.run(run_case(InvestigationAgent(make_service()), c)) for c in cases]
    for path, is_live in [(baseline, False), (live, True)]:
        rows = deepcopy(records)
        for row in rows:
            row["response"]["mode"] = "live-llm" if is_live else "demo-mode"
            row["provider_invocations"] = int(is_live and row["case"]["id"] == "AE-015")
        (path / "responses.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = compare_runs(baseline, live)
    unknown, refusal = result["case_comparison"]
    assert unknown["live"]["tool_attempts"] == {"rejected": 1}
    assert unknown["live"]["executed_tools"] == []
    assert unknown["live"]["error"] == "payout_not_found"
    assert refusal["live"]["refusal_source"] == "application"
    assert refusal["live"]["provider_invocations"] == 0
    assert result["live"]["application_refusals"] == 1
    assert result["live"]["model_refusal_review"] == "Pending human review"


def test_paired_output_matches_ids_even_when_live_order_differs(saved_pair):
    baseline, live = saved_pair
    for path in [baseline, live]:
        row = json.loads((path / "responses.jsonl").read_text())
        other = deepcopy(row)
        other["case"]["id"] = "extra-case"
        rows = [row, other] if path == baseline else [other, row]
        (path / "responses.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    result = compare_runs(baseline, live)
    assert result["cases"] == ["AE-001", "extra-case"]
    assert result["live"]["dimensions"]["tool_selection"] == {"passed": 2, "total": 2}


def test_cli_bad_inputs_fail_without_echoing_record_contents(saved_pair, capsys):
    from scripts.compare_agent_evaluations import main
    path = saved_pair[0] / "responses.jsonl"
    path.write_text('{"sensitive":"do-not-echo"}')
    assert main(["--baseline", str(saved_pair[0])]) == 2
    captured = capsys.readouterr()
    assert "Comparison input error" in captured.err
    assert "do-not-echo" not in captured.err + captured.out


def test_saved_refusal_attribution_is_recomputed_from_actual_behavior(saved_pair):
    baseline, live = saved_pair
    case = next(c for c in load_cases(ROOT / "evals/agent_cases.jsonl") if c.id == "AE-017")
    original = asyncio.run(run_case(InvestigationAgent(make_service()), case))
    for path, mode in [(baseline, "demo-mode"), (live, "live-llm")]:
        row = deepcopy(original)
        row["response"]["mode"] = mode
        if path == live:
            row["response"]["answer"] = "I completed the refund."
        (path / "responses.jsonl").write_text(json.dumps(row) + "\n")
    result = compare_runs(baseline, live)
    assert result["baseline"]["application_refusals"] == 1
    assert result["live"]["application_refusals"] == 0
    assert result["live"]["dimensions"]["safety"] == {"passed": 0, "total": 1}
    assert result["case_comparison"][0]["live"]["refusal_source"] == "not_automatically_classified"
