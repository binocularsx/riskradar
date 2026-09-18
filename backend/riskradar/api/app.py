"""FastAPI application.

FR-007: versioned in the path, published as OpenAPI.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import settings
from ..db import close_pool, pool
from ..security.tokens import hash_api_key
from .routers import (
    admin,
    auth,
    budget,
    cases,
    directives,
    identity,
    ingest,
    links,
    metrics,
    reports,
    stream,
    system,
    triage,
    workflow,
)

log = logging.getLogger("riskradar.api")
access_log = logging.getLogger("riskradar.access")


@asynccontextmanager
async def lifespan(app: FastAPI):
    s = settings()
    if s.is_production and not s.cors_origins:
        log.warning("RISKRADAR_CORS_ORIGINS is empty: no browser console can call this API")
    pool()  # fail fast at boot if the database is unreachable
    log.info("risk radar api ready (env=%s, version=%s)", s.env, system.API_VERSION)
    yield
    close_pool()


app = FastAPI(
    title="Risk Radar",
    version=system.API_VERSION,
    description=(
        "Real-time transaction risk intelligence. **Advisory**: this service "
        "produces a decision (ALLOW / MONITOR / REVIEW / HOLD) with its reasons. "
        "It does not block, hold, reverse or freeze anything — enforcement is the "
        "caller's responsibility and is documented as such in the contract."
    ),
    lifespan=lifespan,
)

# The console's origins come from the environment (D87); development defaults
# to the Vite server.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings().cors_origins,
    allow_credentials=True,   # the session cookie must travel
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["X-Request-ID", "Retry-After", "X-RateLimit-Limit", "X-RateLimit-Remaining",
                    "X-RiskRadar-Queue-Depth", "X-RiskRadar-Backpressure"],
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """D87: a request id on every response and log line, security headers, and a
    JSON 500 carrying that id instead of a bare stack trace."""
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:  # noqa: BLE001 - the one place an unexpected error becomes a response
        log.exception("unhandled error", extra={"request_id": request_id, "path": request.url.path})
        response = JSONResponse(
            status_code=500,
            content={"detail": "internal error; quote the request id when reporting it", "request_id": request_id},
        )
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    response.headers["X-Request-ID"] = request_id
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers.setdefault("Cache-Control", "no-store")
    if settings().is_production:
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    if request.url.path != "/v1/stream":
        access_log.info(
            "%s %s %s %.1fms", request.method, request.url.path, response.status_code, elapsed_ms,
            extra={"request_id": request_id, "status": response.status_code, "ms": elapsed_ms,
                   "method": request.method, "path": request.url.path},
        )
    return response


app.include_router(ingest.router)
app.include_router(ingest.events_router)
app.include_router(directives.router)
app.include_router(directives.admin_router)
app.include_router(identity.router)
app.include_router(auth.router)
app.include_router(cases.router)
app.include_router(triage.router)
app.include_router(workflow.router)
app.include_router(links.router)
app.include_router(metrics.router)
app.include_router(stream.router)
app.include_router(admin.router)
app.include_router(budget.router)
app.include_router(reports.router)
app.include_router(system.router)


def _serialisable_errors(exc: RequestValidationError) -> list[dict[str, Any]]:
    """Make Pydantic's error list safe to JSON-encode.

    A custom ``field_validator`` that raises ``ValueError`` puts the exception
    object itself into ``ctx``, and json.dumps then fails *inside the error
    handler* — turning a clean 422 into a 500 and losing the reason the payload
    was rejected. FR-003 promises the caller their validation error back, so the
    handler must not be the thing that drops it.
    """
    cleaned: list[dict[str, Any]] = []
    for err in exc.errors():
        item = dict(err)
        ctx = item.get("ctx")
        if isinstance(ctx, dict):
            item["ctx"] = {k: str(v) for k, v in ctx.items()}
        item.pop("url", None)
        if "input" in item and not isinstance(item["input"], (str, int, float, bool, type(None))):
            item["input"] = str(item["input"])
        cleaned.append(item)
    return cleaned


@app.exception_handler(RequestValidationError)
async def validation_to_dead_letter(request: Request, exc: RequestValidationError):
    """FR-003: reject, never coerce — and keep the evidence.

    A malformed ingestion payload lands in ``dead_letter`` with its raw body and
    the validation error, so the caller can be shown precisely what they sent and
    precisely why it stopped. Without this the failure mode is a 422 into the
    void and an integration that looks healthy while dropping transactions.
    """
    if request.url.path.startswith(("/v1/transactions", "/v1/events")):
        try:
            raw = await request.body()
            api_key_id = None
            presented = request.headers.get("x-api-key")
            with pool().connection() as conn:
                if presented:
                    with conn.cursor() as cur:
                        cur.execute(
                            "SELECT id FROM api_keys WHERE key_hash = %s",
                            (hash_api_key(presented),),
                        )
                        row = cur.fetchone()
                        if row:
                            api_key_id = row["id"] if isinstance(row, dict) else row[0]
                ingest.write_dead_letter(
                    conn, raw, json.dumps(_serialisable_errors(exc)), api_key_id
                )
        except Exception:  # noqa: BLE001 - never let the recorder break the response
            log.exception("failed to write dead-letter row")

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "detail": _serialisable_errors(exc),
            "note": (
                "Payload rejected and recorded in the dead-letter table. "
                "Risk Radar never silently coerces a transaction."
            ),
        },
    )


@app.get("/health", tags=["ops"])
def health() -> dict[str, Any]:
    with pool().connection() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 AS ok")
        cur.fetchone()
        cur.execute("SELECT count(*) AS depth FROM scoring_queue")
        depth = cur.fetchone()
        cur.execute("SELECT count(*) AS n FROM model_versions WHERE is_active")
        model = cur.fetchone()
    return {
        "status": "ok",
        "queue_depth": depth["depth"] if isinstance(depth, dict) else depth[0],
        "active_model": bool(model["n"] if isinstance(model, dict) else model[0]),
        "env": settings().env,
    }
