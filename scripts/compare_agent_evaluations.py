"""Compare saved demo/live runs offline; never construct an agent or provider client."""

import argparse
from collections import Counter
import json
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from app.agent import AgentResponse
from scripts.evaluate_agent import Case, classify_refusal_source, json_equal, score_case, strict_json, summarize

NOT_EVALUATED = "Not yet evaluated"
INPUT_FILES = {"evals/agent_cases.jsonl", "data/ledger.csv", "data/bank.csv", "data/expected_labels.csv"}


def load_run(directory: Path, mode: str) -> tuple[list[dict], dict]:
    """Validate enough of the existing saved format to safely reuse its scorer."""
    manifest = strict_json((directory / "manifest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("mode") != mode:
        raise ValueError("saved manifest has the wrong execution mode")
    hashes = manifest.get("sha256")
    if not isinstance(hashes, dict) or not INPUT_FILES.issubset(hashes):
        raise ValueError("saved manifest lacks dataset/fixture hashes")
    if any(not isinstance(hashes[name], str) or len(hashes[name]) != 64 or
           any(c not in "0123456789abcdef" for c in hashes[name]) for name in INPUT_FILES):
        raise ValueError("invalid saved input hash")
    if mode == "live" and (not isinstance(manifest.get("model"), str) or not manifest["model"].strip()):
        raise ValueError("live manifest requires configured model identity")
    records = []
    ids = set()
    for line in (directory / "responses.jsonl").read_text(encoding="utf-8").splitlines():
        try:
            record = strict_json(line)
            case = Case.model_validate(record["case"])
            response = AgentResponse.model_validate(record["response"])
            expected_mode = "demo-mode" if mode == "demo" else "live-llm"
            if response.mode != expected_mode:
                raise ValueError("saved response has the wrong mode")
            if case.id in ids:
                raise ValueError("duplicate saved case ID")
            calls, latency = record["provider_invocations"], record["elapsed_ms"]
            if type(calls) is not int or calls < 0 or (mode == "demo" and calls != 0):
                raise ValueError("invalid provider invocation count")
            if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
                raise ValueError("invalid saved latency")
            if record["refusal_source"] not in {"application", "not_automatically_classified"}:
                raise ValueError("invalid refusal attribution")
            if not isinstance(record["attempts"], list):
                raise ValueError("saved tool attempts must be a list")
            for attempt in record["attempts"]:
                if not isinstance(attempt, dict) or not {"name", "arguments", "outcome"}.issubset(attempt):
                    raise ValueError("invalid saved tool attempt")
                if attempt["outcome"] not in {"executed", "rejected", "error"}:
                    raise ValueError("unfinished or invalid saved tool attempt")
            # Recompute objective scores; stale/tampered summary.json is not authoritative.
            record["refusal_source"] = classify_refusal_source(response.answer, record["attempts"], calls)
            record["scores"] = score_case(case, record)
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid saved evaluation record") from exc
        ids.add(case.id)
        records.append(record)
    if not records:
        raise ValueError("saved evaluation has no cases")
    if "selected_case_ids" in manifest and manifest["selected_case_ids"] != [r["case"]["id"] for r in records]:
        raise ValueError("manifest selected case IDs disagree with saved records")
    return records, manifest


def side_summary(records: list[dict], manifest: dict) -> dict:
    result = summarize(records)
    result["model"] = manifest.get("model") if manifest["mode"] == "live" else None
    result["narrative_review"] = {"status": "Pending", "eligible": len(records)}
    result["model_refusal_review"] = "Pending human review" if manifest["mode"] == "live" else "No model invoked"
    result["eligible_case_ids"] = {
        name: [r["case"]["id"] for r in records if r["scores"][name] is not None]
        for name in result["dimensions"]
    }
    result["provider_errors"] = sum(r["response"]["error"] == "provider_error" for r in records)
    result["timeouts"] = sum(r["response"]["error"] == "agent_timeout" for r in records)
    return result


def case_summary(record: dict) -> dict:
    return {
        "scores": record["scores"],
        "tool_attempts": dict(Counter(a["outcome"] for a in record["attempts"])),
        "executed_tools": [tool["name"] for tool in record["response"]["executed_tools"]],
        "provider_invocations": record["provider_invocations"],
        "refusal_source": record["refusal_source"], "error": record["response"]["error"],
    }


def compare_runs(baseline_dir: Path, live_dir: Path | None = None) -> dict:
    baseline, baseline_manifest = load_run(baseline_dir, "demo")
    live, live_manifest = (None, None) if live_dir is None else load_run(live_dir, "live")
    live_by_id = {} if live is None else {r["case"]["id"]: r for r in live}
    ids = [r["case"]["id"] for r in baseline]
    if live is not None:
        if set(ids) != set(live_by_id):
            raise ValueError("paired runs must contain the same case IDs")
        if any(baseline_manifest["sha256"][name] != live_manifest["sha256"][name] for name in INPUT_FILES):
            raise ValueError("paired runs must use the same dataset and fixture hashes")
        for record in baseline:
            if not json_equal(record["case"], live_by_id[record["case"]["id"]]["case"]):
                raise ValueError("paired questions, expectations, and review guidance must agree")
        # Paired output always follows baseline order, independent of live execution order.
        live = [live_by_id[case_id] for case_id in ids]
    return {
        "cases": ids,
        "baseline": side_summary(baseline, baseline_manifest),
        "live": NOT_EVALUATED if live is None else side_summary(live, live_manifest),
        "case_comparison": [
            {"id": record["case"]["id"], "baseline": case_summary(record),
             "live": NOT_EVALUATED if live is None else case_summary(live_by_id[record["case"]["id"]])}
            for record in baseline
        ],
        "limitations": [
            "Human narrative judgments are not imported; all remain Pending in this summary.",
            "Provider error counts and evidence agreement are separate; neither judges explanation truth.",
            "Model identity is the configured model recorded by the runner, not a verified provider snapshot.",
            "Demo latency excludes network/model work; live latency includes provider work when invoked.",
            "Manifests pin inputs, not scorer or application revisions; use the same code revision for paired runs.",
        ],
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="Saved demo run directory")
    parser.add_argument("--live", type=Path, help="Matching saved live run; omit until evaluated")
    args = parser.parse_args(argv)
    try:
        comparison = compare_runs(args.baseline, args.live)
    except (OSError, ValueError) as exc:
        # Avoid echoing arbitrary input records, provider payloads, or filesystem contents.
        print(f"Comparison input error ({type(exc).__name__}); check saved files, modes, case definitions, and hashes.", file=sys.stderr)
        return 2
    print(json.dumps(comparison, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
