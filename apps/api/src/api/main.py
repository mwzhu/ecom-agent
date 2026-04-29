from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from api import __version__
from api.config import get_settings
from api.db.session import dispose_async_engines
from api.routes import agent_runs, cases, evals, integrations, me
from api.webhooks import router as webhooks_router
from ecom_shared import HealthResponse, ServiceName


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    try:
        yield
    finally:
        await dispose_async_engines()


app = FastAPI(
    title="Ecom Agent API",
    version=__version__,
    summary="Phase 0 API shell for ecommerce operations agents.",
    lifespan=lifespan,
)

app.include_router(me.router)
app.include_router(cases.router)
app.include_router(evals.router)
app.include_router(integrations.router)
app.include_router(agent_runs.router)
app.include_router(webhooks_router)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    get_settings()
    return HealthResponse(service=ServiceName.API, status="ok", version=__version__)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": ServiceName.API.value, "status": "ok"}
