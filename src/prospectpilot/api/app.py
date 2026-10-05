"""FastAPI application."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from prospectpilot.memory import db
from prospectpilot.obs import metrics as _metrics  # noqa: F401  (registers collectors)
from prospectpilot.obs.tracing import setup_tracing, shutdown_tracing


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    setup_tracing("prospectpilot-api")
    yield
    shutdown_tracing()
    await db.dispose()


app = FastAPI(title="ProspectPilot", version="0.1.0", lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "db": "ok" if await db.ping() else "unreachable"}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
