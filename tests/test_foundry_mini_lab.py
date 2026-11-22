"""Offline checks: these tests never authenticate or call a model."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_default_command_prepares_five_cases_without_credentials():
    result = subprocess.run(
        [sys.executable, str(ROOT / "experiments/foundry/mini_lab.py")],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    prepared = json.loads(result.stdout)
    assert prepared["mode"] == "dry-run"
    assert len(prepared["request"]["input"]) > 0
    assert "expected" not in prepared["request"]["input"]
    assert prepared["case_count"] == 5


def test_wrong_or_added_facts_are_not_scored_as_correct():
    from experiments.foundry.mini_lab import evaluate
    cases = [{"id": "x", "expected": {"decision": "match", "facts": {"net": 9750}}}]
    scored = evaluate(cases, {"answers": [
        {"id": "x", "decision": "match", "facts": {"net": 9600, "cause": "fraud"}}
    ]})
    assert scored[0]["factual_correct"] is False
    assert scored[0]["unsupported_claims"] == 2


def test_refusal_is_scored_independently_of_factual_answer():
    from experiments.foundry.mini_lab import evaluate
    cases = [{"id": "safety", "safety": True,
              "expected": {"decision": "refuse", "facts": {}}}]
    result = evaluate(cases, {"answers": [
        {"id": "safety", "decision": "match", "facts": {}}
    ]})
    assert result[0]["safety_pass"] is False


@pytest.mark.parametrize("answers", [[], [
    {"id": "x", "decision": "match", "facts": {}},
    {"id": "x", "decision": "match", "facts": {}},
], [{"id": "other", "decision": "match", "facts": {}}]])
def test_missing_duplicate_or_unknown_case_fails_closed(answers):
    from experiments.foundry.mini_lab import evaluate
    with pytest.raises(ValueError):
        evaluate([{"id": "x", "expected": {"decision": "match", "facts": {}}}],
                 {"answers": answers})


def test_endpoint_rejects_token_exfiltration_destinations():
    from experiments.foundry.mini_lab import validate_endpoint
    for endpoint in ["https://example.com/api/projects/x",
                     "https://x.services.ai.azure.com.evil.test/api/projects/x",
                     "http://x.services.ai.azure.com/api/projects/x"]:
        with pytest.raises(ValueError):
            validate_endpoint(endpoint)


def test_live_failure_is_not_retried(tmp_path):
    from experiments.foundry.mini_lab import run_live
    calls = []

    def denied(*args):
        calls.append(1)
        raise TimeoutError("may already have been processed")

    with pytest.raises(TimeoutError):
        run_live([], {}, tmp_path / "result.json", denied)
    assert len(calls) == 1
    assert not (tmp_path / "result.json").exists()
    with pytest.raises(FileExistsError):
        run_live([], {}, tmp_path / "result.json", denied)
    assert len(calls) == 1


def test_incomplete_response_cannot_be_reported_as_success():
    from experiments.foundry.mini_lab import parse_response
    with pytest.raises(ValueError):
        parse_response({"status": "incomplete", "output": []})


def test_success_preserves_aggregate_usage_and_individual_scores(tmp_path):
    from experiments.foundry.mini_lab import run_live
    cases = [{"id": "x", "expected": {"decision": "match", "facts": {"net": 9750}}}]
    response = {"status": "completed", "model": "gpt-5-mini-2025-08-07",
                "usage": {"input_tokens": 100, "output_tokens": 50},
                "output": [{"type": "message", "content": [{"type": "output_text",
                    "text": '{"answers":[{"id":"x","decision":"match","facts":{"net":9750}}]}'}]}]}
    result = run_live(cases, {"model": "gpt-5-mini-2025-08-07"},
                      tmp_path / "result.json", lambda body: response)
    assert result["usage"] == {"input_tokens": 100, "output_tokens": 50}
    assert result["scores"][0]["factual_correct"] is True
    assert result["scores"][0]["unsupported_claims"] == 0
    assert result["aggregate_latency_seconds"] >= 0
    assert json.loads((tmp_path / "result.json").read_text()) == result
