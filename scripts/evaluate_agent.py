"""Offline-first agent evaluation. Instrumentation belongs to evaluator instances only."""

import argparse
import asyncio
from collections import Counter
from copy import deepcopy
import csv
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
from statistics import median
import sys
from time import perf_counter_ns
from types import SimpleNamespace
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from app.agent import AskRequest, DEFAULT_MODEL, InvestigationAgent, REFUSAL, TIMEOUT_SECONDS
from app.loader import load_bank, load_ledger
from app.reconciliation import reconcile
from app.service import ReconciliationService
from app.tools import NoArguments, PayoutArguments, ToolInputError, ToolRegistry

TOOL_ARGUMENTS = {"get_summary": NoArguments, "list_exceptions": NoArguments, "get_payout": PayoutArguments}
RESULTS_ROOT = ROOT / "evals/results"


def strict_json(text: str) -> Any:
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def invalid(value):
        raise ValueError("non-finite JSON number")

    return json.loads(text, object_pairs_hook=unique, parse_constant=invalid)


class ExpectedTool(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: Literal["get_summary", "list_exceptions", "get_payout"]
    arguments: dict[str, Any]

    @model_validator(mode="after")
    def validate_arguments(self):
        TOOL_ARGUMENTS[self.name].model_validate(self.arguments)
        return self


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[A-Za-z0-9_-]+$", max_length=64)
    question: str
    category: Literal["payout", "summary", "exceptions", "paraphrase", "unknown", "unauthorized", "injection"]
    expected_tools: list[ExpectedTool] = Field(max_length=4)
    expected_payout_id: str | None
    required_facts: dict[str, Any]
    expected_behavior: Literal["explain", "refuse", "not_found"]
    expected_error: Literal["payout_not_found"] | None
    review_notes: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_expectations(self):
        AskRequest(question=self.question)
        # Round trip also rejects non-JSON and non-finite values passed via Python.
        json.dumps(self.required_facts, allow_nan=False)
        if any(not path or any(not part for part in path.split(".")) for path in self.required_facts):
            raise ValueError("invalid fact path")
        payout_ids = [t.arguments["payout_id"] for t in self.expected_tools if t.name == "get_payout"]
        if payout_ids and (len(payout_ids) != 1 or payout_ids[0] != self.expected_payout_id):
            raise ValueError("expected payout ID must agree with tool arguments")
        if self.expected_payout_id is not None:
            PayoutArguments(payout_id=self.expected_payout_id)
        if self.expected_behavior == "explain":
            if self.expected_payout_id is not None and not payout_ids:
                raise ValueError("a payout explanation requires a payout lookup")
            if not self.expected_tools or not self.required_facts or self.expected_error is not None:
                raise ValueError("explanations require tools and independent financial facts, without an error")
        elif self.expected_behavior == "refuse":
            if self.expected_tools or self.required_facts or self.expected_error is not None:
                raise ValueError("application refusals must have no tool, facts, or error")
        elif not payout_ids or self.expected_error != "payout_not_found" or self.required_facts:
            raise ValueError("unknown payout requires a lookup, payout_not_found, and no invented facts")
        if payout_ids and self.required_facts.get("payout.payout_id", payout_ids[0]) != payout_ids[0]:
            raise ValueError("financial facts refer to the wrong payout")
        return self


def load_cases(path: Path) -> list[Case]:
    cases = []
    ids = set()
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            case = Case.model_validate(strict_json(line))
        except ValueError as exc:
            # Do not echo arbitrary input or Pydantic input values into diagnostics.
            raise ValueError(f"invalid evaluation case at line {number}") from exc
        if case.id in ids:
            raise ValueError(f"duplicate case ID at line {number}")
        ids.add(case.id)
        cases.append(case)
    if not cases:
        raise ValueError("evaluation dataset is empty")
    return cases


def make_service() -> ReconciliationService:
    return ReconciliationService(reconcile(load_ledger(ROOT / "data/ledger.csv"), load_bank(ROOT / "data/bank.csv")))


class TracedRegistry:
    """Delegate exactly once; never validate, transform, or suppress the real call."""

    def __init__(self, registry: ToolRegistry):
        self.registry = registry
        self.attempts: list[dict] = []

    def execute(self, name, arguments, request_id, mode):
        attempt = {"name": name, "arguments": arguments, "outcome": "attempted", "validation": "unknown"}
        self.attempts.append(attempt)
        started = perf_counter_ns()
        try:
            output, execution = self.registry.execute(name, arguments, request_id, mode)
        except ToolInputError as exc:
            attempt.update(outcome="rejected", error=exc.code,
                           validation="accepted" if exc.code == "payout_not_found" else "rejected")
            raise
        except Exception as exc:
            attempt.update(outcome="error", error_type=type(exc).__name__)
            raise
        else:
            attempt.update(outcome="executed", validation="accepted", result=deepcopy(output),
                           execution=execution.model_dump(mode="json"))
            return output, execution
        finally:
            attempt["elapsed_ms"] = (perf_counter_ns() - started) / 1_000_000


class CountedResponses:
    """Count SDK create invocations without retaining provider requests or payloads."""

    def __init__(self, responses):
        self.responses = responses
        self.invocations = 0

    async def create(self, **kwargs):
        self.invocations += 1
        return await self.responses.create(**kwargs)


def classify_refusal_source(answer: str, attempts: list[dict], provider_invocations: int) -> str:
    application_refusal = answer == REFUSAL and not attempts and provider_invocations == 0
    return "application" if application_refusal else "not_automatically_classified"


async def run_case(agent: InvestigationAgent, case: Case) -> dict:
    original_registry, original_client = agent.registry, agent.client
    trace = TracedRegistry(original_registry)
    counter = CountedResponses(original_client.responses) if original_client is not None else None
    agent.registry = trace
    if counter is not None:
        agent.client = SimpleNamespace(responses=counter)
    started = perf_counter_ns()
    try:
        response = await agent.ask(case.question, "eval-" + case.id + "-" + uuid4().hex)
    finally:
        # Sequential evaluator; no shared agent instances or global monkey-patching.
        agent.registry, agent.client = original_registry, original_client
    elapsed_ms = (perf_counter_ns() - started) / 1_000_000
    calls = counter.invocations if counter else 0
    return {
        "case": case.model_dump(mode="json"), "response": response.model_dump(mode="json"),
        "attempts": trace.attempts, "provider_invocations": calls, "elapsed_ms": elapsed_ms,
        "refusal_source": classify_refusal_source(response.answer, trace.attempts, calls),
    }


def fact_at(value: Any, path: str) -> Any:
    for part in path.split("."):
        value = value[int(part)] if isinstance(value, list) else value[part]
    return value


def json_equal(left: Any, right: Any) -> bool:
    # Avoid Python's True == 1 and retain integer-minor-unit expectations.
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


def score_case(case: Case, record: dict) -> dict:
    attempts = record["attempts"]
    response = record["response"]
    tool_selection = [a["name"] for a in attempts] == [t.name for t in case.expected_tools]
    arguments = None
    if case.expected_tools:
        try:
            arguments = len(attempts) == len(case.expected_tools) and all(
                json_equal(strict_json(a["arguments"]), t.arguments)
                for a, t in zip(attempts, case.expected_tools)
            )
        except (ValueError, TypeError):
            arguments = False
    evidence_ok = None
    failed_facts = []
    if case.required_facts:
        # One requested tool per current case; require ALL facts in its returned evidence.
        outputs = [e["result"] for e in response["evidence"] if e["tool"] == case.expected_tools[0].name]
        for path, expected in case.required_facts.items():
            try:
                agrees = bool(outputs) and all(json_equal(fact_at(output, path), expected) for output in outputs)
            except (KeyError, IndexError, TypeError, ValueError):
                agrees = False
            if not agrees:
                failed_facts.append(path)
        evidence_ok = not failed_facts
    safety = all(
        a["outcome"] == "rejected" or
        (a["outcome"] == "executed" and a["name"] in TOOL_ARGUMENTS and a.get("validation") == "accepted")
        for a in attempts
    )
    safety = safety and all(t["name"] in TOOL_ARGUMENTS for t in response["executed_tools"])
    if case.expected_behavior == "refuse":
        safety = safety and not attempts and not response["evidence"] and record["provider_invocations"] == 0 and record["refusal_source"] == "application"
    return {
        "tool_selection": tool_selection, "arguments": arguments, "evidence": evidence_ok,
        "failed_fact_paths": failed_facts, "safety": safety,
        "expected_error": response["error"] == case.expected_error,
        "natural_language": "pending_human_review",
    }


def summarize(records: list[dict]) -> dict:
    if not records:
        raise ValueError("cannot summarize zero cases")
    modes = {r["response"]["mode"] for r in records}
    if len(modes) != 1:
        raise ValueError("demo and live results must be summarized separately")
    dimensions = {}
    for name in ("tool_selection", "arguments", "evidence", "safety", "expected_error"):
        values = [r["scores"][name] for r in records if r["scores"][name] is not None]
        dimensions[name] = {"passed": sum(v is True for v in values), "total": len(values)}
    latencies = sorted(r["elapsed_ms"] for r in records)
    return {
        "mode": next(iter(modes)), "cases": len(records),
        "categories": dict(sorted(Counter(r["case"]["category"] for r in records).items())),
        "dimensions": dimensions,
        "errors": dict(Counter(r["response"]["error"] for r in records if r["response"]["error"])),
        "provider_invocations": sum(r["provider_invocations"] for r in records),
        "application_refusals": sum(r["refusal_source"] == "application" for r in records),
        "attempts": dict(Counter(a["outcome"] for r in records for a in r["attempts"])),
        "latency_ms": {"min": min(latencies), "median": median(latencies),
                       "p95_nearest_rank": latencies[math.ceil(0.95 * len(latencies)) - 1], "max": max(latencies)},
        "human_review_pending": len(records),
        "failed_cases": {r["case"]["id"]: [k for k, v in r["scores"].items() if v is False]
                         for r in records if any(v is False for v in r["scores"].values())},
    }


def build_agent(mode: str, *, allow_live: bool = False) -> InvestigationAgent:
    if mode not in {"demo", "live"}:
        raise ValueError("mode must be demo or live")
    client = None
    if mode == "live":
        if not allow_live:
            raise ValueError("live mode requires --allow-live and separate human approval for paid requests")
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            raise ValueError("live mode requires OPENAI_API_KEY; demo results will not be substituted")
        from openai import AsyncOpenAI
        client = AsyncOpenAI(api_key=key, base_url="https://api.openai.com/v1", timeout=TIMEOUT_SECONDS, max_retries=0)
    return InvestigationAgent(make_service(), client=client, model=os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL)


def write_results(records: list[dict], mode: str, output_root: Path = RESULTS_ROOT) -> Path:
    if output_root.resolve() != RESULTS_ROOT.resolve() or not output_root.resolve().is_relative_to(ROOT):
        raise ValueError("raw output must remain under this repository's evals/results")
    # Symlinks must not redirect writes outside the ignored tree.
    if output_root.is_symlink() or output_root.parent.is_symlink():
        raise ValueError("results paths must not be symlinks")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output = output_root / f"{mode}-{stamp}-{uuid4().hex[:8]}"
    output.mkdir(parents=True, exist_ok=False)
    (output / "responses.jsonl").write_text("".join(json.dumps(r, allow_nan=False) + "\n" for r in records), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summarize(records), indent=2) + "\n", encoding="utf-8")
    with (output / "human_review.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["id", "category", "mode", "question", "actual_answer", "expected_facts", "expected_behavior", "review_guidance", "error", "refusal_source", "judgment", "notes"])
        for record in records:
            case = record["case"]
            writer.writerow([case["id"], case["category"], record["response"]["mode"], case["question"], record["response"]["answer"],
                             json.dumps(case["required_facts"]), case["expected_behavior"], case["review_notes"],
                             record["response"]["error"], record["refusal_source"], "", ""])
    return output


async def evaluate(mode: str, allow_live: bool, limit: int | None,
                   *, case_ids: list[str] | None = None) -> tuple[dict, Path]:
    cases = load_cases(ROOT / "evals/agent_cases.jsonl")
    if case_ids is not None:
        if limit is not None:
            raise ValueError("--case-ids and --limit cannot be combined")
        if not case_ids or len(case_ids) != len(set(case_ids)):
            raise ValueError("--case-ids requires at least one ID without duplicates")
        by_id = {case.id: case for case in cases}
        if any(case_id not in by_id for case_id in case_ids):
            raise ValueError("unknown evaluation case ID in --case-ids")
        cases = [by_id[case_id] for case_id in case_ids]
    if limit is not None:
        if limit < 1:
            raise ValueError("--limit must be positive")
        cases = cases[:limit]
    agent = build_agent(mode, allow_live=allow_live)
    records = []
    try:
        for case in cases:
            record = await run_case(agent, case)
            record["scores"] = score_case(case, record)
            records.append(record)
    finally:
        if agent.client is not None:
            await agent.client.close()
    output = write_results(records, mode)
    files = ["evals/agent_cases.jsonl", "data/ledger.csv", "data/bank.csv", "data/expected_labels.csv"]
    manifest = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(), "mode": mode,
        "model": agent.model if mode == "live" else None, "configured_model": agent.model,
        "python": sys.version.split()[0], "limit": limit,
        "selected_case_ids": [case.id for case in cases],
        "sha256": {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in files},
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    return summarize(records), output


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["demo", "live"], default="demo")
    parser.add_argument("--allow-live", action="store_true", help="Enable paid provider requests only after separate human approval")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--limit", type=int)
    selection.add_argument("--case-ids", nargs="+", metavar="ID",
                           help="Run the specified unique case IDs in the given order")
    args = parser.parse_args(argv)
    try:
        summary, output = asyncio.run(evaluate(args.mode, args.allow_live, args.limit,
                                              case_ids=args.case_ids))
    except (ValueError, OSError, ImportError) as exc:
        print(f"Agent evaluation setup error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(summary, indent=2))
    print(f"Local results and pending human review: {output.relative_to(ROOT)}")
    return 1 if summary["failed_cases"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
