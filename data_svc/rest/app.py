"""FastAPI application factory.

The factory pattern (rather than a module-level `app = FastAPI()`) is
required so tests can build isolated instances against an ephemeral
Postgres, and so the OpenAPI drift test can call create_app().openapi()
on demand.

Lifespan:
  1. Open the Postgres pool.
  2. Instantiate AssetsRepo, BarCache, QuoteService and stash on app.state.
  3. Run the assets.yaml sync.
  4. On shutdown — close the pool.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..db.assets import AssetsRepo
from ..db.cache import BarCache
from ..db.postgres import close_pool
from ..services import assets_loader
from ..services.quote import QuoteService
from .routers import healthz, historical, profile, quote, search
from .settings import RestSettings

logger = logging.getLogger(__name__)


def _read_openapi_overrides() -> dict:
    """Pull info.title / info.version from docs/openapi.yaml so the runtime
    OpenAPI doc exactly matches the committed file. Best effort — falls
    back to defaults if the file isn't readable from the container."""
    import yaml  # local import; not always needed
    candidates = [
        Path(__file__).resolve().parent.parent.parent / "docs" / "openapi.yaml",
        Path("/app/docs/openapi.yaml"),
    ]
    for p in candidates:
        if p.exists():
            try:
                spec = yaml.safe_load(p.read_text())
                return {
                    "title": spec.get("info", {}).get("title", "hybrid-data-svc REST API"),
                    "version": spec.get("info", {}).get("version", "0.1.0"),
                }
            except Exception as exc:  # pragma: no cover
                logger.warning("could not parse %s: %s", p, exc)
    return {"title": "hybrid-data-svc REST API", "version": "0.1.0"}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings: RestSettings = app.state.settings

    assets_repo = AssetsRepo(settings.postgres_url)
    bar_cache = BarCache(settings.postgres_url)
    quote_service = QuoteService(assets_repo, bar_cache)

    app.state.assets_repo = assets_repo
    app.state.bar_cache = bar_cache
    app.state.quote_service = quote_service

    try:
        n = assets_loader.sync(assets_repo, settings.assets_yaml_path, strict=False)
        logger.info("REST gateway ready; %d asset(s) in catalog", n)
    except Exception as exc:
        # Don't kill the gateway on a bad YAML — healthz will report
        # assets_loaded=false and operators can fix the file.
        logger.error("assets sync failed at startup: %s", exc)

    try:
        yield
    finally:
        close_pool()


def create_app(settings: Optional[RestSettings] = None) -> FastAPI:
    if settings is None:
        settings = RestSettings()

    meta = _read_openapi_overrides()
    app = FastAPI(
        title=meta["title"],
        version=meta["version"],
        lifespan=lifespan,
        servers=[
            {
                "url": "http://localhost:8001",
                "description": "Local docker-compose default.",
            }
        ],
    )
    app.state.settings = settings

    # CORS: wildcard origin with allow_credentials=False (browsers reject
    # the combo otherwise). Documented in docs/openapi.yaml header.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=False,
        allow_methods=["GET"],
        allow_headers=["Authorization", "Content-Type"],
    )

    app.include_router(healthz.router)
    app.include_router(quote.router)
    app.include_router(historical.router)
    app.include_router(search.router)
    app.include_router(profile.router)

    # Unify error responses on the documented ErrorResponse shape when the
    # route raised with detail as a dict; FastAPI's default would wrap the
    # dict in a top-level {"detail": ...}.
    @app.exception_handler(StarletteHTTPException)
    async def _http_exc_handler(_request, exc: StarletteHTTPException):  # noqa: ANN001
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail and "message" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        # Fallback to FastAPI's default wrapping.
        return JSONResponse(status_code=exc.status_code, content={"detail": detail})

    @app.exception_handler(RequestValidationError)
    async def _validation_exc_handler(_request, exc: RequestValidationError):  # noqa: ANN001
        # Keep FastAPI's HTTPValidationError shape — the drift test asserts
        # that schema is in docs/openapi.yaml.
        return JSONResponse(status_code=422, content={"detail": exc.errors()})

    return app
