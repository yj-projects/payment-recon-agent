"""HTTP response contracts; domain and matching modules remain transport-free."""

from typing import Literal

from pydantic import BaseModel

from app.models import (
    BankTransaction, LedgerPayout, ReconciliationEvidence, ReconciliationResult,
    Status, Summary,
)


class HealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class PayoutResponse(BaseModel):
    payout: LedgerPayout
    status: Status
    candidate_bank_txn_ids: tuple[str, ...]
    candidates: tuple[BankTransaction, ...]
    evidence: ReconciliationEvidence
    reason: str

    @classmethod
    def from_result(cls, result: ReconciliationResult) -> "PayoutResponse":
        return cls(
            payout=result.payout, status=result.status,
            candidate_bank_txn_ids=result.candidate_bank_txn_ids,
            candidates=result.candidates, evidence=result.evidence, reason=result.reason,
        )


class ReconcileResponse(BaseModel):
    summary: Summary
    results: tuple[PayoutResponse, ...]


class ExceptionsResponse(BaseModel):
    payout_exceptions: tuple[PayoutResponse, ...]
    unmatched_bank: tuple[BankTransaction, ...]
