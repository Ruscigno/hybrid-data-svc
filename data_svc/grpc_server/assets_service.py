"""AssetService gRPC implementation.

Backs the REST routes that touch the curated catalog:
  - /v1/profile, /v1/search (read-only, Phase 1)
  - /v1/assets GET + POST (Phase 3 catalog management)

Rows are populated at gRPC server startup from data_svc/assets.yaml (see
__main__.py for the loader call); runtime additions go through CreateAsset.
"""
from __future__ import annotations

import logging
import re

import grpc

from ..db.assets import AssetRow, AssetsRepo, AssetWithStatusRow
from .proto import bars_pb2 as _pb
from .proto import bars_pb2_grpc as _pb_grpc

logger = logging.getLogger(__name__)

_MAX_SEARCH_LIMIT = 50
_DEFAULT_SEARCH_LIMIT = 10

_MAX_LIST_LIMIT = 500
_DEFAULT_LIST_LIMIT = 100

# Approximate hint surfaced in CreateAssetResponse.poll_eta_seconds.
# data-svc re-reads polling targets at the top of every cycle; on a quiet
# bar (no fetches required) the cycle settles into the default sleep
# (poll_interval_s ≈ 30s). So new feeds are observed within one cycle.
_POLL_ETA_SECONDS = 30

_VALID_ASSET_CLASSES = {"EQUITY", "CRYPTO", "ETF", "FUND"}
# TradingView identifier shape — uppercase exchange : uppercase symbol with
# limited punctuation (e.g. BINANCE:BTCUSDT, NASDAQ:GOOGL, BMFBOVESPA:PETR4).
_TV_SYMBOL_RE = re.compile(r"^[A-Z0-9]+:[A-Z0-9._-]+$")

# Server default timeframes when the caller passes an empty list.
_DEFAULT_TIMEFRAMES = ("1h",)


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


def _row_to_asset_with_status_message(row: AssetWithStatusRow) -> _pb.AssetWithStatus:
    return _pb.AssetWithStatus(
        asset=_row_to_asset_message(row.asset),
        status=row.status,
        added_at=row.asset.added_at,
        last_bar_ts=row.last_bar_ts,
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

    def ListAssets(self, request, context):  # noqa: N802
        # Validate + clamp limit. proto3 int32 defaults to 0 when omitted;
        # treat that as "use server default".
        requested = int(request.limit) if request.limit else _DEFAULT_LIST_LIMIT
        limit = max(1, min(requested, _MAX_LIST_LIMIT))

        rows, next_cursor = self._repo.list_with_status(
            exchange=request.exchange or None,
            asset_class=request.asset_class or None,
            q=request.query or None,
            cursor=request.cursor or None,
            limit=limit,
        )
        return _pb.ListAssetsResponse(
            assets=[_row_to_asset_with_status_message(r) for r in rows],
            next_cursor=next_cursor or "",
        )

    def CreateAsset(self, request, context):  # noqa: N802
        asset_msg = request.asset
        # Required fields (per spec) — empty string is the proto3 default,
        # so explicit emptiness is how we detect missing.
        if not asset_msg.symbol:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("asset.symbol is required")
            return _pb.CreateAssetResponse()
        if not _TV_SYMBOL_RE.match(asset_msg.symbol):
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(
                "asset.symbol must match TradingView identifier shape "
                "EXCHANGE:SYMBOL (uppercase + [._-])"
            )
            return _pb.CreateAssetResponse()
        if not asset_msg.storage_symbol:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("asset.storage_symbol is required")
            return _pb.CreateAssetResponse()
        if not asset_msg.name:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("asset.name is required")
            return _pb.CreateAssetResponse()
        if not asset_msg.exchange:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("asset.exchange is required")
            return _pb.CreateAssetResponse()
        if not asset_msg.currency:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details("asset.currency is required")
            return _pb.CreateAssetResponse()
        if asset_msg.asset_class not in _VALID_ASSET_CLASSES:
            context.set_code(grpc.StatusCode.INVALID_ARGUMENT)
            context.set_details(
                f"asset.asset_class must be one of "
                f"{sorted(_VALID_ASSET_CLASSES)}"
            )
            return _pb.CreateAssetResponse()

        # Server defaults: empty list → ["1h"]; empty tv_symbol → asset.symbol.
        timeframes = list(request.timeframes) or list(_DEFAULT_TIMEFRAMES)
        tv_symbol = request.tv_symbol or asset_msg.symbol

        asset_row = AssetRow(
            symbol=asset_msg.symbol,
            storage_symbol=asset_msg.storage_symbol,
            name=asset_msg.name,
            exchange=asset_msg.exchange,
            currency=asset_msg.currency,
            asset_class=asset_msg.asset_class,
            asset_subclass=asset_msg.asset_subclass or None,
            isin=asset_msg.isin or None,
            country=asset_msg.country or None,
        )

        try:
            created_or_existing, created = self._repo.create_with_feeds(
                asset_row, timeframes, tv_symbol,
            )
        except Exception:
            logger.exception("CreateAsset: create_with_feeds failed")
            context.set_code(grpc.StatusCode.INTERNAL)
            context.set_details("internal error creating asset")
            return _pb.CreateAssetResponse()

        if not created:
            # Symbol already existed: return its current runtime state so the
            # caller can decide whether to treat this as a no-op or surface a
            # 409 (REST adapter does the latter).
            existing = self._repo.get_with_status(created_or_existing.symbol)
            if existing is None:
                # Shouldn't happen — we just held a FOR UPDATE lock on it.
                logger.error(
                    "CreateAsset: symbol %s vanished between conflict detection "
                    "and re-read; treating as internal error",
                    created_or_existing.symbol,
                )
                context.set_code(grpc.StatusCode.INTERNAL)
                context.set_details("inconsistent catalog state")
                return _pb.CreateAssetResponse()
            return _pb.CreateAssetResponse(
                asset_with_status=_row_to_asset_with_status_message(existing),
                created=False,
                poll_eta_seconds=_POLL_ETA_SECONDS,
            )

        # Fresh insert: every feed row was just created with status='pending'
        # and no bars exist yet, so we can synthesize the wrapper without an
        # extra round-trip.
        fresh = AssetWithStatusRow(
            asset=created_or_existing,
            status="pending",
            last_bar_ts=0,
        )
        return _pb.CreateAssetResponse(
            asset_with_status=_row_to_asset_with_status_message(fresh),
            created=True,
            poll_eta_seconds=_POLL_ETA_SECONDS,
        )


def build_servicer(postgres_url: str) -> AssetServiceServicer:
    repo = AssetsRepo(postgres_url)
    return AssetServiceServicer(repo)
