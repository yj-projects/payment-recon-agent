"""Read-only queries over a reconciliation snapshot; no transport dependencies."""

from dataclasses import dataclass

from app.models import ExceptionReport, ReconciliationBatch, ReconciliationResult, Status, Summary


class PayoutNotFoundError(LookupError):
    """Requested payout does not exist in this batch."""


@dataclass(frozen=True)
class ReconciliationService:
    batch: ReconciliationBatch

    def get_summary(self) -> Summary:
        matched = sum(row.status == Status.MATCHED for row in self.batch.results)
        total = len(self.batch.results)
        return Summary(total, matched, total - matched, len(self.batch.unmatched_bank))

    def list_exceptions(self) -> ExceptionReport:
        return ExceptionReport(
            tuple(row for row in self.batch.results if row.status != Status.MATCHED),
            self.batch.unmatched_bank,
        )

    def get_payout(self, payout_id: str) -> ReconciliationResult:
        for result in self.batch.results:
            if result.payout.payout_id == payout_id:
                return result
        raise PayoutNotFoundError(f"Unknown payout: {payout_id}")
