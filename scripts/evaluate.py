"""Evaluate the committed synthetic fixture; never used for application decisions."""

import argparse
from collections import Counter
import csv
from dataclasses import asdict
import json
from pathlib import Path
import sys

# Permit `python /absolute/path/scripts/evaluate.py` from any working directory.
ROOT = Path(__file__).resolve().parents[1]
if __package__ in (None, ""):
    sys.path.insert(0, str(ROOT))

from app.loader import load_bank, load_ledger
from app.models import InputValidationError, Status
from app.reconciliation import reconcile
from app.service import ReconciliationService

LABEL_FIELDS = {"payout_id", "expected_status", "expected_bank_txn_ids", "expected_reason"}
EXPECTED_SUMMARY = {"total_payouts": 12, "matched": 7, "exceptions": 5, "unmatched_bank": 1}


def _load_labels(path: Path) -> dict[str, dict[str, str]]:
    labels = {}
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, strict=True)
            header = reader.fieldnames
            if header is None or len(header) != len(LABEL_FIELDS) or set(header) != LABEL_FIELDS:
                raise InputValidationError(f"{path}: invalid golden header")
            for row in reader:
                if None in row or any(value is None for value in row.values()):
                    raise InputValidationError(f"{path}:{reader.line_num}: malformed golden row")
                payout_id = row["payout_id"]
                if not payout_id or payout_id != payout_id.strip() or payout_id in labels:
                    raise InputValidationError(f"{path}:{reader.line_num}: invalid or duplicate golden payout_id")
                if row["expected_status"] not in {status.value for status in Status}:
                    raise InputValidationError(f"{path}:{reader.line_num}: unknown golden status")
                ids = row["expected_bank_txn_ids"].split(";") if row["expected_bank_txn_ids"] else []
                if any(not value or value != value.strip() for value in ids) or len(ids) != len(set(ids)):
                    raise InputValidationError(f"{path}:{reader.line_num}: invalid golden candidate IDs")
                labels[payout_id] = row
    except (OSError, UnicodeError, csv.Error) as exc:
        raise InputValidationError(f"cannot read golden labels {path}: {exc}") from exc
    return labels


def evaluate(data_dir: Path, labels_path: Path | None = None) -> dict:
    """Report fixture agreement, including explicit seed-count and stray-ID checks."""
    ledger = load_ledger(data_dir / "ledger.csv")
    bank = load_bank(data_dir / "bank.csv")
    batch = reconcile(ledger, bank)
    labels = _load_labels(labels_path if labels_path is not None else data_dir / "expected_labels.csv")
    discrepancies = []
    actual_results = []
    correct = 0
    actual_ids = {row.payout.payout_id for row in batch.results}
    for payout_id in sorted(set(labels) - actual_ids):
        discrepancies.append(f"{payout_id}: golden label has no actual payout")
    for result in batch.results:
        payout_id = result.payout.payout_id
        actual_results.append({
            "payout_id": payout_id,
            "status": result.status.value,
            "candidate_bank_txn_ids": list(result.candidate_bank_txn_ids),
            "reason": result.reason,
            "evidence": asdict(result.evidence),
        })
        expected = labels.get(payout_id)
        if expected is None:
            discrepancies.append(f"{payout_id}: missing golden label")
            continue
        status_ok = result.status.value == expected["expected_status"]
        expected_ids = set(filter(None, expected["expected_bank_txn_ids"].split(";")))
        candidates_ok = set(result.candidate_bank_txn_ids) == expected_ids
        if not status_ok:
            discrepancies.append(f"{payout_id}: status actual={result.status.value} expected={expected['expected_status']}")
        if not candidates_ok:
            discrepancies.append(f"{payout_id}: candidate IDs actual={list(result.candidate_bank_txn_ids)} expected={sorted(expected_ids)}")
        correct += int(status_ok and candidates_ok)
    summary = asdict(ReconciliationService(batch).get_summary())
    if summary != EXPECTED_SUMMARY:
        discrepancies.append(f"seed summary actual={summary} expected={EXPECTED_SUMMARY}")
    unmatched_ids = [row.bank_txn_id for row in batch.unmatched_bank]
    if unmatched_ids != ["B-999"]:
        discrepancies.append(f"unmatched bank IDs actual={unmatched_ids} expected=['B-999']")
    return {
        "dataset": {"ledger_rows": len(ledger), "bank_rows": len(bank), "golden_rows": len(labels)},
        "correct_payouts": correct,
        "total_payouts": len(batch.results),
        "summary": summary,
        "status_counts": dict(sorted(Counter(row.status.value for row in batch.results).items())),
        "unmatched_bank_txn_ids": unmatched_ids,
        "actual_results": actual_results,
        "discrepancies": discrepancies,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate the seeded synthetic fixture, not real-world accuracy.")
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data")
    parser.add_argument("--labels", type=Path, help="Override golden labels for evaluation only")
    args = parser.parse_args()
    try:
        report = evaluate(args.data_dir, args.labels)
    except InputValidationError as exc:
        print(f"Evaluation input error: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    print(f"Golden fixture agreement: {report['correct_payouts']}/{report['total_payouts']} correct")
    if report["discrepancies"]:
        print(f"Discrepancies: {len(report['discrepancies'])} (see report above)")
    else:
        print("Discrepancies: none")
    return 1 if report["discrepancies"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
