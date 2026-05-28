"""FastAPI DI factories.

The repos and services are constructed once at app startup and stashed on
`app.state`; these helpers expose them as dependencies so routes can use
the standard FastAPI `Depends(get_X)` pattern.
"""

from __future__ import annotations

from fastapi import Request

from ..db.assets import AssetsRepo
from ..db.cache import BarCache
from .grpc_client import BarServiceClient


def get_settings(request: Request):
    return request.app.state.settings


def get_assets_repo(request: Request) -> AssetsRepo:
    return request.app.state.assets_repo


def get_bar_cache(request: Request) -> BarCache:
    return request.app.state.bar_cache


def get_grpc_client(request: Request) -> BarServiceClient:
    return request.app.state.grpc_client
