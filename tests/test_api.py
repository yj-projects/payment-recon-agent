from dataclasses import asdict
import json
from pathlib import Path
import shutil
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from fastapi.testclient import TestClient
import pytest

from app.main import create_app
from app.models import InputValidationError
from app.observability import JsonFormatter
from app.service import ReconciliationService

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client():
    with TestClient(create_app()) as client:
        yield client


def test_health_and_generated_request_ids(client):
    first = client.get("/health", headers={"X-Request-ID": "untrusted"})
    second = client.get("/health")
    assert first.status_code == 200
    assert first.json() == {"status": "ok"}
    assert UUID(first.headers["X-Request-ID"]).version == 4
    assert first.headers["X-Request-ID"] != second.headers["X-Request-ID"]


def test_reconcile_agrees_with_authoritative_service(client):
    response = client.get("/reconcile")
    assert response.status_code == 200
    body = response.json()
    service = client.app.state.service
    assert body["summary"] == asdict(service.get_summary())
    assert body["summary"] == {"total_payouts": 12, "matched": 7, "exceptions": 5, "unmatched_bank": 1}
    assert len(body["results"]) == 12
    for result in body["results"]:
        expected = service.get_payout(result["payout"]["payout_id"])
        assert result["status"] == expected.status.value
        assert result["candidate_bank_txn_ids"] == list(expected.candidate_bank_txn_ids)
        assert result["evidence"] == jsonable_encoder(expected.evidence)
        assert result["payout"] == jsonable_encoder(expected.payout)
        assert result["candidates"] == jsonable_encoder(expected.candidates)


def test_exceptions_keep_unmatched_bank_separate(client):
    body = client.get("/exceptions").json()
    assert [r["payout"]["payout_id"] for r in body["payout_exceptions"]] == ["P-003", "P-004", "P-005", "P-006", "P-007"]
    assert [r["bank_txn_id"] for r in body["unmatched_bank"]] == ["B-999"]
    assert all("bank_txn_id" not in r for r in body["payout_exceptions"])
    assert body["unmatched_bank"] == jsonable_encoder(client.app.state.service.list_exceptions().unmatched_bank)


def test_payout_evidence_and_not_found(client):
    mismatch = client.get("/payouts/P-003").json()
    assert mismatch["status"] == "AMOUNT_MISMATCH"
    assert mismatch["evidence"]["amount_difference_cents"] == -50
    assert mismatch["payout"]["expected_amount_cents"] == 120000
    assert mismatch["candidates"][0]["actual_amount_cents"] == 119950
    duplicate = client.get("/payouts/P-005").json()
    assert duplicate["status"] == "DUPLICATE_CANDIDATES"
    assert duplicate["candidate_bank_txn_ids"] == ["B-005A", "B-005B"]
    assert duplicate["evidence"]["amount_difference_cents"] is None
    missing = client.get("/payouts/P-404")
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Payout not found"}
    assert missing.headers["X-Request-ID"]


def test_page_and_generated_docs_available(client):
    page = client.get("/")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/reconcile" in schema["paths"]
    assert "/agent/ask" in schema["paths"]


def test_snapshot_is_not_reloaded_for_requests(tmp_path):
    shutil.copytree(ROOT / "data", tmp_path / "data")
    with TestClient(create_app(data_dir=tmp_path / "data")) as client:
        snapshot = client.app.state.service.batch
        (tmp_path / "data/ledger.csv").write_text("invalid after startup")
        assert client.get("/reconcile").json()["summary"]["matched"] == 7
        assert client.get("/exceptions").status_code == 200
        assert client.get("/payouts/P-003").status_code == 200
        assert client.app.state.service.batch is snapshot


def test_bad_startup_input_fails_explicitly(tmp_path, caplog):
    caplog.set_level("INFO", logger="payment_recon")
    with pytest.raises(InputValidationError):
        with TestClient(create_app(data_dir=tmp_path)):
            pass
    errors = [r for r in caplog.records if r.name == "payment_recon" and r.getMessage() == "startup_failed"]
    assert len(errors) == 1
    assert errors[0].error_type == "InputValidationError"


def test_json_request_logs_include_metadata_but_exclude_secrets(client, caplog):
    caplog.set_level("INFO", logger="payment_recon")
    response = client.get("/health?api_key=secret-query", headers={"Authorization": "Bearer secret-header"})
    record = next(r for r in caplog.records if r.name == "payment_recon" and r.getMessage() == "http_request")
    text = JsonFormatter().format(record)
    event = json.loads(text)
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert event["method"] == "GET"
    assert event["path"] == "/health"
    assert event["status"] == 200
    assert event["duration_ms"] >= 0
    assert event["level"] == "INFO"
    assert "secret-query" not in text
    assert "secret-header" not in text


def test_unexpected_application_error_is_sanitized_and_logged(client, monkeypatch, caplog):
    def broken_summary(self):
        raise RuntimeError("secret-provider-message")
    monkeypatch.setattr(ReconciliationService, "get_summary", broken_summary)
    caplog.set_level("INFO", logger="payment_recon")
    response = client.get("/reconcile")
    assert response.status_code == 500
    assert response.json() == {"detail": "Internal server error"}
    assert response.headers["X-Request-ID"]
    record = next(r for r in caplog.records if r.name == "payment_recon" and r.levelname == "ERROR")
    event = json.loads(JsonFormatter().format(record))
    assert event["status"] == 500
    assert event["error_type"] == "RuntimeError"
    assert event["request_id"] == response.headers["X-Request-ID"]
    assert "secret-provider-message" not in JsonFormatter().format(record)
