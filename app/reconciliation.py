"""Pure matching rules in PROJECT_SPEC.md order; no file or network access."""

from collections import defaultdict
from collections.abc import Iterable

from app.models import (
    BankTransaction, LedgerPayout, ReconciliationBatch, ReconciliationEvidence,
    ReconciliationResult, Status, validate_bank, validate_ledger,
)


def _classify(payout: LedgerPayout, candidates: tuple[BankTransaction, ...]) -> ReconciliationResult:
    difference = None
    offset = None
    if not candidates:
        status, rule, reason = Status.MISSING_IN_BANK, "R2_MISSING_IN_BANK", "No bank candidate for merchant and normalized reference"
    elif len(candidates) > 1:
        status, rule = Status.DUPLICATE_CANDIDATES, "R3_DUPLICATE_CANDIDATES"
        reason = f"{len(candidates)} bank candidates share merchant and normalized reference; manual review required"
    else:
        bank = candidates[0]
        offset = (bank.posted_date - payout.expected_date).days
        if bank.currency != payout.currency:
            status, rule = Status.CURRENCY_MISMATCH, "R4_CURRENCY_MISMATCH"
            reason = f"Ledger currency {payout.currency} differs from bank currency {bank.currency}"
        else:
            difference = bank.actual_amount_cents - payout.expected_amount_cents
            if difference != 0:
                status, rule = Status.AMOUNT_MISMATCH, "R5_AMOUNT_MISMATCH"
                reason = (
                    f"Actual {bank.actual_amount_cents} cents vs expected {payout.expected_amount_cents} cents; "
                    f"difference {difference:+d} cents (bank minus expected)"
                )
            elif not 0 <= offset <= 2:
                status, rule = Status.DATE_OUT_OF_WINDOW, "R6_DATE_OUT_OF_WINDOW"
                reason = f"Bank date offset {offset:+d} calendar days; accepted offsets are 0 through 2 inclusive"
            else:
                status, rule = Status.MATCHED, "R7_MATCHED"
                reason = f"Merchant, normalized reference, currency and amount match; date offset {offset} calendar days"
    evidence = ReconciliationEvidence(rule, payout.merchant_id, payout.normalized_reference, len(candidates), difference, offset)
    return ReconciliationResult(payout, status, candidates, evidence, reason)


def reconcile(ledger: Iterable[LedgerPayout], bank: Iterable[BankTransaction]) -> ReconciliationBatch:
    """Reject unsafe batches, retain every candidate, and return stable ID order."""
    payouts = tuple(ledger)
    transactions = tuple(bank)
    # Validate here too: callers need not have used the CSV loader.
    validate_ledger(payouts)
    validate_bank(transactions)
    index: dict[tuple[str, str], list[BankTransaction]] = defaultdict(list)
    for transaction in sorted(transactions, key=lambda row: row.bank_txn_id):
        index[transaction.merchant_id, transaction.normalized_reference].append(transaction)
    associated_ids: set[str] = set()
    results = []
    for payout in sorted(payouts, key=lambda row: row.payout_id):
        candidates = tuple(index.get((payout.merchant_id, payout.normalized_reference), ()))
        associated_ids.update(row.bank_txn_id for row in candidates)
        results.append(_classify(payout, candidates))
    unmatched = tuple(sorted(
        (row for row in transactions if row.bank_txn_id not in associated_ids),
        key=lambda row: row.bank_txn_id,
    ))
    return ReconciliationBatch(tuple(results), unmatched)
