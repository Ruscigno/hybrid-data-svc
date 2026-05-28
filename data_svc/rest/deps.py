"""FastAPI DI factories.

The gRPC client is constructed once at app startup (see app.py::lifespan)
and stashed on `app.state`. Every route depends on this single instance
via `Depends(get_grpc_client)`. REST holds no other backing state — no
Postgres pool, no in-process repo.
"""

from __future__ import annotations

from fastapi import Request

from .grpc_client import BarServiceClient


def get_settings(request: Request):
    return request.app.state.settings


def get_grpc_client(request: Request) -> BarServiceClient:
    return request.app.state.grpc_client
