"""Strict CSV boundary. Golden labels are deliberately absent from this module."""

import csv
from dataclasses import fields
from datetime import date
from pathlib import Path
import re
from typing import TypeVar

from app.models import BankTransaction, InputValidationError, LedgerPayout, validate_bank, validate_ledger

Transaction = TypeVar("Transaction", LedgerPayout, BankTransaction)


def _load(path: Path, model: type[Transaction], amount_field: str, date_field: str) -> tuple[Transaction, ...]:
    path = Path(path)
    expected_fields = {field.name for field in fields(model)}
    records = []
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            header = reader.fieldnames
            if header is None or len(header) != len(expected_fields) or set(header) != expected_fields:
                raise InputValidationError(f"{path}: invalid header; expected columns {sorted(expected_fields)}")
            for row in reader:
                try:
                    if None in row or any(value is None for value in row.values()):
                        raise InputValidationError("row has missing or extra columns")
                    if not re.fullmatch(r"-?[0-9]+", row[amount_field]):
                        raise InputValidationError(f"{amount_field} must be integer minor units")
                    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", row[date_field]):
                        raise InputValidationError(f"{date_field} must use YYYY-MM-DD")
                    values = dict(row)
                    values[amount_field] = int(row[amount_field])
                    values[date_field] = date.fromisoformat(row[date_field])
                    records.append(model(**values))
                except (ValueError, TypeError) as exc:
                    raise InputValidationError(f"{path}:{reader.line_num}: {exc}") from exc
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputValidationError(f"cannot read {path}: {exc}") from exc
    return tuple(records)


def load_ledger(path: Path) -> tuple[LedgerPayout, ...]:
    records = _load(path, LedgerPayout, "expected_amount_cents", "expected_date")
    validate_ledger(records)
    return records


def load_bank(path: Path) -> tuple[BankTransaction, ...]:
    records = _load(path, BankTransaction, "actual_amount_cents", "posted_date")
    validate_bank(records)
    return records
