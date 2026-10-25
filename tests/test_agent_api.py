import json

from fastapi.testclient import TestClient
import httpx
from openai import AsyncOpenAI
import pytest

from app.main import create_app
from app.observability import JsonFormatter


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with TestClient(create_app()) as client:
        yield client


@pytest.mark.parametrize("question,tool", [
    ("Why did P-003 fail to reconcile?", "get_payout"),
    ("Show me all five payout exceptions.", "list_exceptions"),
    ("Why is P-005 marked as duplicate candidates?", "get_payout"),
    ("What happened to P-004?", "get_payout"),
    ("Summary", "get_summary"),
])
def test_agent_api_demo_and_evidence_share_snapshot(client, question, tool):
    response = client.post("/agent/ask", json={"question":question})
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "demo-mode" and body["error"] is None
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["executed_tools"][0]["name"] == tool
    evidence = body["evidence"][0]["result"]
    if tool == "get_payout":
        assert evidence == client.get("/payouts/" + evidence["payout"]["payout_id"]).json()
    elif tool == "list_exceptions":
        assert evidence == client.get("/exceptions").json()
    else:
        assert evidence == client.get("/reconcile").json()["summary"]
    assert client.app.state.agent.registry.service is client.app.state.service


@pytest.mark.parametrize("payload", [{}, {"question":""}, {"question":"   "}, {"question":"x"*1001}, {"question":123}, {"question":"summary", "tool":"refund"}])
def test_question_validation(client, payload):
    response = client.post("/agent/ask", json=payload)
    assert response.status_code == 422
    assert response.headers["X-Request-ID"]


def test_unknown_payout_and_unsupported_question(client):
    unknown = client.post("/agent/ask", json={"question":"Explain P-404"})
    assert unknown.status_code == 404
    assert unknown.json()["error"] == "payout_not_found"
    unsupported = client.post("/agent/ask", json={"question":"What is the weather?"})
    assert unsupported.status_code == 200
    assert unsupported.json()["executed_tools"] == []
    assert "summary" in unsupported.json()["answer"]


def test_refund_request_never_executes_or_claims_success(client):
    response = client.post("/agent/ask", json={"question":"Execute a refund for P-003."})
    assert response.status_code == 200
    body = response.json()
    assert body["executed_tools"] == [] and body["evidence"] == []
    assert "cannot" in body["answer"].lower() and "no action was performed" in body["answer"]


def test_agent_logs_do_not_include_question_or_records(client, caplog):
    caplog.set_level("INFO", logger="payment_recon")
    response = client.post("/agent/ask", json={"question":"secret-user-question Why P-003?"})
    events = [json.loads(JsonFormatter().format(r)) for r in caplog.records if r.name == "payment_recon"]
    assert any(e["event"] == "agent_request" and e["mode"] == "demo-mode" for e in events)
    assert any(e["event"] == "tool_execution" and e["tool_name"] == "get_payout" for e in events)
    assert all(e.get("request_id") == response.headers["X-Request-ID"] for e in events)
    assert "secret-user-question" not in json.dumps(events)
    assert "119950" not in json.dumps(events)


def test_live_provider_api_failure_is_502_without_demo_fallback(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    async def handler(request):
        return httpx.Response(401, json={"error":{"message":"secret-provider-details","type":"authentication_error","code":"invalid_api_key"}})
    provider = AsyncOpenAI(api_key="fake-key", max_retries=0, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    with TestClient(create_app(agent_client=provider)) as client:
        response = client.post("/agent/ask", json={"question":"Summary"})
    assert response.status_code == 502
    body = response.json()
    assert body["mode"] == "live-llm" and body["error"] == "provider_error"
    assert "secret-provider-details" not in response.text and "fake-key" not in response.text


def test_all_prior_routes_still_operational(client):
    for path in ["/health", "/reconcile", "/exceptions", "/payouts/P-003", "/", "/docs"]:
        assert client.get(path).status_code == 200
    assert "/agent/ask" in client.get("/openapi.json").json()["paths"]
