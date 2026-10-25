"""Exactly three validated, read-only capabilities over the startup service."""

from dataclasses import asdict
import json
from time import perf_counter_ns
from typing import Literal

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.api_models import ExceptionsResponse, PayoutResponse
from app.observability import LOGGER
from app.service import PayoutNotFoundError, ReconciliationService

ToolName = Literal["get_summary", "list_exceptions", "get_payout"]


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class PayoutArguments(NoArguments):
    payout_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


_ARGUMENT_MODELS = {"get_summary": NoArguments, "list_exceptions": NoArguments, "get_payout": PayoutArguments}
_DESCRIPTIONS = {
    "get_summary": "Read authoritative payout and unmatched-bank counts from the synthetic reconciliation snapshot.",
    "list_exceptions": "Read all payout exceptions with evidence, plus a separate unmatched_bank list.",
    "get_payout": "Read one payout's authoritative status, candidates, rule identifier, amounts and evidence. Never changes it.",
}
TOOL_SCHEMAS = [
    {"type": "function", "name": name, "description": _DESCRIPTIONS[name], "strict": True,
     "parameters": {**model.model_json_schema(), "required": list(model.model_fields)}}
    for name, model in _ARGUMENT_MODELS.items()
]


class ToolExecution(BaseModel):
    name: ToolName
    outcome: Literal["success"] = "success"
    duration_ms: int


class ToolInputError(ValueError):
    def __init__(self, code: str = "invalid_tool_request"):
        self.code = code
        super().__init__(code)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ToolInputError()
        result[key] = value
    return result


def _reject_constant(value):
    raise ToolInputError()


class ToolRegistry:
    def __init__(self, service: ReconciliationService):
        self.service = service

    def execute(self, name: str, arguments: str, request_id: str, mode: str) -> tuple[dict, ToolExecution]:
        started = perf_counter_ns()
        outcome = "rejected"
        try:
            if not isinstance(name, str) or name not in _ARGUMENT_MODELS or not isinstance(arguments, str) or len(arguments) > 4096:
                raise ToolInputError()
            try:
                raw = json.loads(arguments, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                args = _ARGUMENT_MODELS[name].model_validate(raw)
            except (ValueError, TypeError, ValidationError, RecursionError) as exc:
                raise ToolInputError() from exc
            # Explicit branches: no getattr, eval, arbitrary imports, or user-selected functions.
            if name == "get_summary":
                result = asdict(self.service.get_summary())
            elif name == "list_exceptions":
                report = self.service.list_exceptions()
                result = ExceptionsResponse(
                    payout_exceptions=tuple(PayoutResponse.from_result(row) for row in report.payout_exceptions),
                    unmatched_bank=report.unmatched_bank,
                ).model_dump(mode="json")
            else:
                try:
                    payout = self.service.get_payout(args.payout_id)
                except PayoutNotFoundError as exc:
                    raise ToolInputError("payout_not_found") from exc
                result = PayoutResponse.from_result(payout).model_dump(mode="json")
            outcome = "success"
            return jsonable_encoder(result), ToolExecution(name=name, duration_ms=(perf_counter_ns() - started) // 1_000_000)
        finally:
            LOGGER.info("tool_execution", extra={
                "request_id": request_id, "mode": mode,
                "tool_name": name if isinstance(name, str) and name in _ARGUMENT_MODELS else "unregistered",
                "outcome": outcome, "duration_ms": (perf_counter_ns() - started) // 1_000_000,
            })
