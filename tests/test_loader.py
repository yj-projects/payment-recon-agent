import csv
from dataclasses import FrozenInstanceError
from datetime import date
from pathlib import Path

import pytest

from app.loader import load_bank, load_ledger
from app.models import BankTransaction, InputValidationError, LedgerPayout, normalize_reference

ROOT = Path(__file__).resolve().parents[1]
LEDGER_FIELDS = ["payout_id", "merchant_id", "currency", "expected_amount_cents", "expected_date", "reference"]


def write_ledger(tmp_path, **changes):
    row = dict(zip(LEDGER_FIELDS, ["P-1", "M-1", "usd", "1200", "2026-10-01", " settle-01 "]))
    row.update(changes)
    path = tmp_path / "ledger.csv"
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=LEDGER_FIELDS)
        writer.writeheader()
        writer.writerow(row)
    return path


@pytest.mark.parametrize("raw, expected", [(" settle_001 - ", "SETTLE001"), ("a.z/09", "AZ09"), ("éß１２", "")])
def test_reference_normalization_is_ascii_only(raw, expected):
    assert normalize_reference(raw) == expected


def test_load_fixtures_preserves_source_and_types():
    ledger = load_ledger(ROOT / "data/ledger.csv")
    bank = load_bank(ROOT / "data/bank.csv")
    assert len(ledger) == 12
    assert len(bank) == 13
    assert ledger[0].expected_amount_cents == 105000
    assert type(ledger[0].expected_amount_cents) is int
    assert ledger[0].expected_date == date(2026, 10, 1)
    assert bank[0].reference == " settle 001 "
    assert bank[0].normalized_reference == "SETTLE001"
    assert bank[-1].bank_txn_id == "B-999"


def test_currency_case_normalized_and_records_immutable(tmp_path):
    payout = load_ledger(write_ledger(tmp_path))[0]
    assert payout.currency == "USD"
    assert payout.reference == " settle-01 "
    with pytest.raises(FrozenInstanceError):
        payout.currency = "EUR"


@pytest.mark.parametrize("changes", [
    {"expected_amount_cents": "12.00"}, {"expected_amount_cents": "1e3"},
    {"expected_amount_cents": ""}, {"expected_date": "2026-02-30"},
    {"expected_date": "20261001"}, {"currency": "US"}, {"currency": "12A"},
    {"payout_id": ""}, {"payout_id": " P-1"}, {"merchant_id": " "},
    {"reference": "---"},
])
def test_invalid_csv_fields_fail_with_row_context(tmp_path, changes):
    with pytest.raises(InputValidationError, match=r"ledger.csv:2:"):
        load_ledger(write_ledger(tmp_path, **changes))


@pytest.mark.parametrize("content", [
    "", "payout_id,payout_id\nP-1,P-2\n", "payout_id\nP-1\n",
    ",".join(LEDGER_FIELDS) + "\nP-1,M-1,USD,100,2026-10-01\n",
    ",".join(LEDGER_FIELDS) + "\nP-1,M-1,USD,100,2026-10-01,R,EXTRA\n",
])
def test_invalid_csv_shape_rejected(tmp_path, content):
    path = tmp_path / "ledger.csv"
    path.write_text(content)
    with pytest.raises(InputValidationError):
        load_ledger(path)


def test_duplicate_ids_and_ledger_matching_keys_rejected(tmp_path):
    path = write_ledger(tmp_path)
    original = path.read_text()
    path.write_text(original + original.splitlines()[1] + "\n")
    with pytest.raises(InputValidationError, match="duplicate payout_id"):
        load_ledger(path)
    path.write_text(original + "P-2,M-1,USD,1300,2026-10-02,SETTLE_01\n")
    with pytest.raises(InputValidationError, match="ambiguous ledger"):
        load_ledger(path)


def test_bank_duplicate_id_rejected_but_duplicate_matching_key_allowed(tmp_path):
    path = tmp_path / "bank.csv"
    header = "bank_txn_id,merchant_id,currency,actual_amount_cents,posted_date,reference\n"
    path.write_text(header + "B-1,M-1,USD,100,2026-10-01,R\nB-2,M-1,USD,100,2026-10-01,R\n")
    assert len(load_bank(path)) == 2
    path.write_text(path.read_text().replace("B-2", "B-1"))
    with pytest.raises(InputValidationError, match="duplicate bank_txn_id"):
        load_bank(path)


@pytest.mark.parametrize("amount", [1.5, True, "100"])
def test_direct_model_construction_rejects_non_integer_money(amount):
    with pytest.raises(InputValidationError):
        LedgerPayout("P-1", "M-1", "USD", amount, date(2026, 10, 1), "R")
    with pytest.raises(InputValidationError):
        BankTransaction("B-1", "M-1", "USD", amount, date(2026, 10, 1), "R")


def test_missing_file_has_explicit_error(tmp_path):
    with pytest.raises(InputValidationError, match="cannot read"):
        load_bank(tmp_path / "missing.csv")


def test_zero_and_signed_integer_amounts_are_preserved(tmp_path):
    assert load_ledger(write_ledger(tmp_path, expected_amount_cents="0"))[0].expected_amount_cents == 0
    assert load_ledger(write_ledger(tmp_path, expected_amount_cents="-50"))[0].expected_amount_cents == -50
