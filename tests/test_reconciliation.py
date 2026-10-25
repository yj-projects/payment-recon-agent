from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.loader import load_bank, load_ledger
from app.models import BankTransaction, InputValidationError, LedgerPayout, Status
from app.reconciliation import reconcile
from app.service import PayoutNotFoundError, ReconciliationService

DAY = date(2026, 10, 1)
PAYOUT = LedgerPayout("P-1", "M-1", "USD", 10000, DAY, "settle-01")
BANK = BankTransaction("B-1", "M-1", "USD", 10000, DAY, "SETTLE_01")
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("days,status", [
    (-1, Status.DATE_OUT_OF_WINDOW), (0, Status.MATCHED),
    (1, Status.MATCHED), (2, Status.MATCHED), (3, Status.DATE_OUT_OF_WINDOW),
])
def test_calendar_date_window_is_inclusive_zero_to_two(days, status):
    result = reconcile([PAYOUT], [replace(BANK, posted_date=DAY + timedelta(days=days))]).results[0]
    assert result.status == status
    assert result.evidence.date_offset_days == days
    assert result.evidence.amount_difference_cents == 0
    assert result.candidate_bank_txn_ids == ("B-1",)


@pytest.mark.parametrize("actual,difference", [(9950, -50), (10050, 50), (0, -10000), (10**25, 10**25 - 10000)])
def test_amount_difference_uses_exact_signed_integer_cents(actual, difference):
    result = reconcile([PAYOUT], [replace(BANK, actual_amount_cents=actual)]).results[0]
    assert result.status == Status.AMOUNT_MISMATCH
    assert result.evidence.amount_difference_cents == difference
    assert type(result.evidence.amount_difference_cents) is int
    assert str(difference) in result.reason


def test_duplicate_precedes_currency_amount_and_date():
    banks = [BANK, replace(BANK, bank_txn_id="B-2", currency="EUR", actual_amount_cents=2, posted_date=DAY - timedelta(days=5))]
    batch = reconcile([PAYOUT], banks)
    result = batch.results[0]
    assert result.status == Status.DUPLICATE_CANDIDATES
    assert result.candidate_bank_txn_ids == ("B-1", "B-2")
    assert result.candidates == tuple(banks)
    assert result.evidence.candidate_count == 2
    assert result.evidence.amount_difference_cents is None
    assert result.evidence.date_offset_days is None
    assert batch.unmatched_bank == ()


def test_currency_precedes_amount_and_date_without_cross_currency_subtraction():
    bank = replace(BANK, currency="eur", actual_amount_cents=2, posted_date=DAY - timedelta(days=5))
    result = reconcile([PAYOUT], [bank]).results[0]
    assert result.status == Status.CURRENCY_MISMATCH
    assert result.evidence.amount_difference_cents is None
    assert result.candidates[0].currency == "EUR"
    assert result.payout == PAYOUT


def test_amount_precedes_date():
    bank = replace(BANK, actual_amount_cents=9950, posted_date=DAY + timedelta(days=10))
    result = reconcile([PAYOUT], [bank]).results[0]
    assert result.status == Status.AMOUNT_MISMATCH
    assert result.evidence.amount_difference_cents == -50
    assert result.evidence.date_offset_days == 10
    assert result.evidence.rule_id == "R5_AMOUNT_MISMATCH"


@pytest.mark.parametrize("bank", [replace(BANK, merchant_id="M-2"), replace(BANK, reference="OTHER")])
def test_candidate_requires_both_merchant_and_normalized_reference(bank):
    batch = reconcile([PAYOUT], [bank])
    result = batch.results[0]
    assert result.status == Status.MISSING_IN_BANK
    assert result.candidate_bank_txn_ids == ()
    assert result.evidence.normalized_reference == "SETTLE01"
    assert result.evidence.amount_difference_cents is None
    assert batch.unmatched_bank == (bank,)


def test_engine_rejects_ambiguous_ledger_keys_even_without_bank_candidates():
    with pytest.raises(InputValidationError, match="ambiguous ledger"):
        reconcile([PAYOUT, replace(PAYOUT, payout_id="P-2", reference="SETTLE_01", currency="EUR")], [])


def test_engine_rejects_duplicate_ids_when_csv_loader_is_bypassed():
    with pytest.raises(InputValidationError, match="duplicate payout_id"):
        reconcile([PAYOUT, replace(PAYOUT, reference="OTHER")], [])
    with pytest.raises(InputValidationError, match="duplicate bank_txn_id"):
        reconcile([PAYOUT], [BANK, BANK])


def test_fixture_summary_exceptions_lookup_and_unmatched_bank():
    batch = reconcile(load_ledger(ROOT / "data/ledger.csv"), load_bank(ROOT / "data/bank.csv"))
    service = ReconciliationService(batch)
    summary = service.get_summary()
    assert (summary.total_payouts, summary.matched, summary.exceptions, summary.unmatched_bank) == (12, 7, 5, 1)
    exceptions = service.list_exceptions()
    assert [r.payout.payout_id for r in exceptions.payout_exceptions] == ["P-003", "P-004", "P-005", "P-006", "P-007"]
    assert [b.bank_txn_id for b in exceptions.unmatched_bank] == ["B-999"]
    assert service.get_payout("P-003").evidence.amount_difference_cents == -50
    assert service.get_payout("P-005").candidate_bank_txn_ids == ("B-005A", "B-005B")
    with pytest.raises(PayoutNotFoundError, match="P-404"):
        service.get_payout("P-404")


def test_empty_batch_and_bank_only_batch():
    empty = ReconciliationService(reconcile([], []))
    assert empty.get_summary().total_payouts == 0
    assert empty.list_exceptions().payout_exceptions == ()
    bank_only = ReconciliationService(reconcile([], [BANK]))
    assert bank_only.get_summary().unmatched_bank == 1


def test_output_order_is_stable_for_input_permutations():
    other = replace(PAYOUT, payout_id="P-2", reference="OTHER")
    banks = [BANK, replace(BANK, bank_txn_id="B-2")]
    assert reconcile([PAYOUT, other], banks) == reconcile([other, PAYOUT], list(reversed(banks)))
