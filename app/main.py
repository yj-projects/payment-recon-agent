"""Local read-only FastAPI application over one startup reconciliation snapshot."""

from collections import Counter
from contextlib import asynccontextmanager
from dataclasses import asdict
import os
from pathlib import Path
from time import perf_counter_ns
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse

from app.api_models import ExceptionsResponse, HealthResponse, PayoutResponse, ReconcileResponse
from app.agent import AgentResponse, AskRequest, DEFAULT_MODEL, InvestigationAgent, TIMEOUT_SECONDS
from app.loader import load_bank, load_ledger
from app.observability import LOGGER, configure_logging
from app.reconciliation import reconcile
from app.service import PayoutNotFoundError, ReconciliationService

ROOT = Path(__file__).resolve().parents[1]


def create_app(data_dir: Path | None = None, *, agent_client=None) -> FastAPI:
    configure_logging()
    fixture_dir = Path(data_dir) if data_dir is not None else ROOT / "data"

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            batch = reconcile(load_ledger(fixture_dir / "ledger.csv"), load_bank(fixture_dir / "bank.csv"))
            app.state.service = ReconciliationService(batch)
            client = agent_client
            api_key = os.environ.get("OPENAI_API_KEY", "").strip()
            if client is None and api_key:
                # Optional SDK is needed only when credentials enable the live path.
                from openai import AsyncOpenAI
                client = AsyncOpenAI(api_key=api_key, base_url="https://api.openai.com/v1", timeout=TIMEOUT_SECONDS, max_retries=0)
            app.state.agent = InvestigationAgent(
                app.state.service, client=client,
                model=os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_MODEL,
            )
        except Exception as exc:
            LOGGER.error("startup_failed", extra={"error_type": type(exc).__name__})
            raise
        LOGGER.info("snapshot_loaded", extra={
            "summary": asdict(app.state.service.get_summary()),
            "status_counts": dict(Counter(result.status.value for result in batch.results)),
        })
        try:
            yield
        finally:
            if client is not None:
                await client.close()

    app = FastAPI(
        title="Synthetic Payment Reconciliation", version="0.3.0",
        description="Read-only deterministic reconciliation of synthetic demonstration data.",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def request_logging(request: Request, call_next):
        request_id = str(uuid4())
        request.state.request_id = request_id
        started = perf_counter_ns()
        error_type = None
        try:
            response = await call_next(request)
        except Exception as exc:
            error_type = type(exc).__name__
            response = JSONResponse(status_code=500, content={"detail": "Internal server error"})
        response.headers["X-Request-ID"] = request_id
        fields = {
            "request_id": request_id,
            "method": request.method,
            "path": request.url.path,
            "status": response.status_code,
            "duration_ms": (perf_counter_ns() - started) // 1_000_000,
        }
        if error_type is not None:
            fields["error_type"] = error_type
        LOGGER.log(40 if response.status_code >= 500 else 20, "http_request", extra=fields)
        return response

    @app.get("/health", response_model=HealthResponse)
    def health() -> HealthResponse:
        return HealthResponse()

    @app.get("/reconcile", response_model=ReconcileResponse)
    def reconciliation(request: Request) -> ReconcileResponse:
        service = request.app.state.service
        return ReconcileResponse(
            summary=service.get_summary(),
            results=tuple(PayoutResponse.from_result(row) for row in service.batch.results),
        )

    @app.get("/exceptions", response_model=ExceptionsResponse)
    def exceptions(request: Request) -> ExceptionsResponse:
        report = request.app.state.service.list_exceptions()
        return ExceptionsResponse(
            payout_exceptions=tuple(PayoutResponse.from_result(row) for row in report.payout_exceptions),
            unmatched_bank=report.unmatched_bank,
        )

    @app.get("/payouts/{payout_id}", response_model=PayoutResponse)
    def payout(payout_id: str, request: Request) -> PayoutResponse:
        try:
            return PayoutResponse.from_result(request.app.state.service.get_payout(payout_id))
        except PayoutNotFoundError:
            raise HTTPException(status_code=404, detail="Payout not found") from None

    @app.get("/", response_class=FileResponse, include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(ROOT / "static/index.html")

    @app.post("/agent/ask", response_model=AgentResponse)
    async def ask_agent(body: AskRequest, request: Request):
        result = await request.app.state.agent.ask(body.question, request.state.request_id)
        if result.error:
            status = 404 if result.error == "payout_not_found" else 504 if result.error == "agent_timeout" else 502
            return JSONResponse(status_code=status, content=result.model_dump(mode="json"))
        return result

    return app


app = create_app()
