import csv
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from scripts.evaluate import evaluate

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def data_dir(tmp_path):
    shutil.copytree(ROOT / "data", tmp_path / "data")
    return tmp_path / "data"


def rewrite_labels(data_dir, transform):
    path = data_dir / "expected_labels.csv"
    with path.open(newline="") as stream:
        reader = csv.DictReader(stream)
        fields = reader.fieldnames
        rows = list(reader)
    rows = transform(rows)
    with path.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_golden_fixture_agreement():
    report = evaluate(ROOT / "data")
    assert report["correct_payouts"] == 12
    assert report["discrepancies"] == []
    assert report["summary"] == {"total_payouts": 12, "matched": 7, "exceptions": 5, "unmatched_bank": 1}
    assert report["unmatched_bank_txn_ids"] == ["B-999"]
    assert report["status_counts"]["MATCHED"] == 7
    assert len(report["actual_results"]) == 12


def test_evaluator_compares_ids_as_sets_and_labels_by_payout_id(data_dir):
    def change(rows):
        rows[4]["expected_bank_txn_ids"] = "B-005B;B-005A"
        return list(reversed(rows))
    rewrite_labels(data_dir, change)
    assert evaluate(data_dir)["discrepancies"] == []


def test_evaluator_reports_wrong_status_and_candidate_ids(data_dir):
    def change(rows):
        rows[2]["expected_status"] = "MATCHED"
        rows[4]["expected_bank_txn_ids"] = "B-005A"
        return rows
    rewrite_labels(data_dir, change)
    report = evaluate(data_dir)
    assert report["correct_payouts"] == 10
    assert any("P-003" in error and "status" in error for error in report["discrepancies"])
    assert any("P-005" in error and "candidate" in error for error in report["discrepancies"])


def test_evaluator_detects_missing_and_extra_labels(data_dir):
    def change(rows):
        rows[0]["payout_id"] = "P-EXTRA"
        return rows
    rewrite_labels(data_dir, change)
    errors = evaluate(data_dir)["discrepancies"]
    assert any("P-001" in error for error in errors)
    assert any("P-EXTRA" in error for error in errors)


def test_cli_runs_outside_repo_without_installing_package(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/evaluate.py")], cwd=tmp_path, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert "12/12 correct" in result.stdout
    assert "Discrepancies: none" in result.stdout
    assert '"B-999"' in result.stdout


def test_cli_returns_nonzero_and_prints_actual_results_for_disagreement(data_dir):
    path = data_dir / "bank.csv"
    path.write_text(path.read_text().replace("119950", "120000"))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/evaluate.py"), "--data-dir", str(data_dir)], text=True, capture_output=True)
    assert result.returncode == 1
    assert "P-003" in result.stdout
    assert "11/12 correct" in result.stdout
    assert '"actual_results"' in result.stdout


@pytest.mark.parametrize("problem", ["duplicate", "unknown_status", "bad_header"])
def test_bad_golden_input_returns_explicit_error(data_dir, problem):
    if problem == "duplicate":
        rewrite_labels(data_dir, lambda rows: rows + [rows[0]])
    elif problem == "unknown_status":
        path = data_dir / "expected_labels.csv"
        path.write_text(path.read_text().replace("AMOUNT_MISMATCH", "UNKNOWN_STATUS"))
    else:
        (data_dir / "expected_labels.csv").write_text("payout_id\nP-1\n")
    result = subprocess.run([sys.executable, str(ROOT / "scripts/evaluate.py"), "--data-dir", str(data_dir)], text=True, capture_output=True)
    assert result.returncode == 2
    assert "Evaluation input error:" in result.stderr


def test_application_works_without_golden_labels(data_dir):
    from app.loader import load_bank, load_ledger
    from app.reconciliation import reconcile
    from app.service import ReconciliationService

    (data_dir / "expected_labels.csv").unlink()
    service = ReconciliationService(reconcile(load_ledger(data_dir / "ledger.csv"), load_bank(data_dir / "bank.csv")))
    assert service.get_summary().matched == 7


def test_wrong_unmatched_bank_identity_is_a_discrepancy(data_dir):
    path = data_dir / "bank.csv"
    path.write_text(path.read_text().replace("B-999", "B-OTHER"))
    report = evaluate(data_dir)
    assert report["correct_payouts"] == 12
    assert any("unmatched bank IDs" in error for error in report["discrepancies"])
