"""Immutable domain contracts and validation, independent of HTTP and LLMs."""

from dataclasses import dataclass
from datetime import date
from enum import Enum
import re


class InputValidationError(ValueError):
    """Input cannot be safely reconciled; no partial batch should be used."""


def normalize_reference(reference: str) -> str:
    """Keep ASCII letters/digits and uppercase; do not transliterate Unicode."""
    return re.sub(r"[^A-Za-z0-9]", "", reference).upper()


def _validate_transaction(record, id_field: str, amount_field: str, date_field: str) -> None:
    for field in (id_field, "merchant_id"):
        value = getattr(record, field)
        if not isinstance(value, str) or not value or value != value.strip():
            raise InputValidationError(f"{field} must be nonempty text without surrounding whitespace")
    if not isinstance(record.currency, str) or not re.fullmatch(r"[A-Za-z]{3}", record.currency):
        raise InputValidationError("currency must be a three-letter ASCII code")
    object.__setattr__(record, "currency", record.currency.upper())
    if type(getattr(record, amount_field)) is not int:
        raise InputValidationError(f"{amount_field} must be integer minor units")
    if type(getattr(record, date_field)) is not date:
        raise InputValidationError(f"{date_field} must be a calendar date")
    if not isinstance(record.reference, str) or not normalize_reference(record.reference):
        raise InputValidationError("reference must contain ASCII letters or digits")


@dataclass(frozen=True)
class LedgerPayout:
    payout_id: str
    merchant_id: str
    currency: str
    expected_amount_cents: int
    expected_date: date
    reference: str

    def __post_init__(self) -> None:
        _validate_transaction(self, "payout_id", "expected_amount_cents", "expected_date")

    @property
    def normalized_reference(self) -> str:
        return normalize_reference(self.reference)


@dataclass(frozen=True)
class BankTransaction:
    bank_txn_id: str
    merchant_id: str
    currency: str
    actual_amount_cents: int
    posted_date: date
    reference: str

    def __post_init__(self) -> None:
        _validate_transaction(self, "bank_txn_id", "actual_amount_cents", "posted_date")

    @property
    def normalized_reference(self) -> str:
        return normalize_reference(self.reference)


class Status(str, Enum):
    MATCHED = "MATCHED"
    MISSING_IN_BANK = "MISSING_IN_BANK"
    DUPLICATE_CANDIDATES = "DUPLICATE_CANDIDATES"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    AMOUNT_MISMATCH = "AMOUNT_MISMATCH"
    DATE_OUT_OF_WINDOW = "DATE_OUT_OF_WINDOW"


@dataclass(frozen=True)
class ReconciliationEvidence:
    rule_id: str
    merchant_id: str
    normalized_reference: str
    candidate_count: int
    amount_difference_cents: int | None
    date_offset_days: int | None


@dataclass(frozen=True)
class ReconciliationResult:
    payout: LedgerPayout
    status: Status
    candidates: tuple[BankTransaction, ...]
    evidence: ReconciliationEvidence
    reason: str

    @property
    def candidate_bank_txn_ids(self) -> tuple[str, ...]:
        return tuple(candidate.bank_txn_id for candidate in self.candidates)


@dataclass(frozen=True)
class ReconciliationBatch:
    results: tuple[ReconciliationResult, ...]
    unmatched_bank: tuple[BankTransaction, ...]


@dataclass(frozen=True)
class Summary:
    total_payouts: int
    matched: int
    exceptions: int
    unmatched_bank: int


@dataclass(frozen=True)
class ExceptionReport:
    payout_exceptions: tuple[ReconciliationResult, ...]
    unmatched_bank: tuple[BankTransaction, ...]


def validate_ledger(records: tuple[LedgerPayout, ...]) -> None:
    ids: set[str] = set()
    keys: dict[tuple[str, str], str] = {}
    for record in records:
        if record.payout_id in ids:
            raise InputValidationError(f"duplicate payout_id: {record.payout_id}")
        ids.add(record.payout_id)
        key = (record.merchant_id, record.normalized_reference)
        if key in keys:
            raise InputValidationError(
                f"ambiguous ledger matching key: {keys[key]} and {record.payout_id}; bank transaction reuse is unsafe"
            )
        keys[key] = record.payout_id


def validate_bank(records: tuple[BankTransaction, ...]) -> None:
    ids: set[str] = set()
    for record in records:
        if record.bank_txn_id in ids:
            raise InputValidationError(f"duplicate bank_txn_id: {record.bank_txn_id}")
        ids.add(record.bank_txn_id)
