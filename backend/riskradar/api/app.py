"""FastAPI application.

FR-007: versioned in the path, published as OpenAPI.
"""

from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from ..config import settings
from ..db import close_pool, pool
from ..security.tokens import hash_api_key
from .routers import admin, auth, cases, ingest, metrics, stream, triage

log = logging.getLogger("riskradar.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool()  # fail fast at boot if the database is unreachable
    log.info("risk radar api ready (env=%s)", settings().env)
    yield
    close_pool()


app = FastAPI(
    title="Risk Radar",
    version="1.0.0",
    description=(
        "Real-time transaction risk intelligence. **Advisory**: this service "
        "produces a decision (ALLOW / MONITOR / REVIEW / HOLD) with its reasons. "
        "It does not block, hold, reverse or freeze anything — enforcement is the "
        "caller's responsibility and is documented as such in the contract."
    ),
    lifespan=lifespan,
)

# The dashboard is served from a different origin in development only.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,   # the session cookie must travel
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(ingest.router)
app.include_router(ingest.events_router)
app.include_router(auth.router)
app.include_router(cases.router)
app.include_router(triage.router)
app.include_router(metrics.router)
app.include_router(stream.router)
app.include_router(admin.router)


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
