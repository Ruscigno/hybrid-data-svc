"""AssetService gRPC implementation.

Backs `/v1/profile` and `/v1/search` REST routes via the AssetsRepo
(Postgres `assets` table). Rows are populated at gRPC server startup
from data_svc/assets.yaml (see __main__.py for the loader call).
"""
from __future__ import annotations

import logging

import grpc

from ..db.assets import AssetRow, AssetsRepo
from .proto import bars_pb2 as _pb
from .proto import bars_pb2_grpc as _pb_grpc

logger = logging.getLogger(__name__)

_MAX_SEARCH_LIMIT = 50
_DEFAULT_SEARCH_LIMIT = 10


def _row_to_asset_message(row: AssetRow) -> _pb.Asset:
    # Empty strings for unset optional fields — proto3 scalar default; the
    # REST adapter omits them from the JSON response.
    return _pb.Asset(
        symbol=row.symbol,
        storage_symbol=row.storage_symbol,
        name=row.name,
        exchange=row.exchange,
        currency=row.currency,
        asset_class=row.asset_class,
        asset_subclass=row.asset_subclass or "",
        isin=row.isin or "",
        country=row.country or "",
    )


class AssetServiceServicer(_pb_grpc.AssetServiceServicer):
    def __init__(self, repo: AssetsRepo) -> None:
        self._repo = repo

    def GetProfile(self, request, context):  # noqa: N802
        symbol = request.symbol
        if not symbol:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("symbol is required")
            return _pb.Asset()
        row = self._repo.get(symbol)
        if row is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"{symbol} is not in the configured feed list")
            return _pb.Asset()
        return _row_to_asset_message(row)

    def Search(self, request, context):  # noqa: N802
        query = (request.query or "").strip()
        requested = int(request.limit) if request.limit else _DEFAULT_SEARCH_LIMIT
        limit = max(1, min(requested, _MAX_SEARCH_LIMIT))
        if not query:
            return _pb.SearchAssetsResponse(results=[])
        rows = self._repo.search(query, limit)
        return _pb.SearchAssetsResponse(
            results=[_row_to_asset_message(r) for r in rows],
        )


def build_servicer(postgres_url: str) -> AssetServiceServicer:
    repo = AssetsRepo(postgres_url)
    return AssetServiceServicer(repo)
