"""/v1/assets — catalog management (Phase 3).

Thin gateway over AssetService.ListAssets / AssetService.CreateAsset. Reads
are gated by `require_bearer`; writes by `require_bearer_admin` (see
data_svc/rest/auth.py for the token-precedence rule).

All business logic — validation, conflict resolution, status aggregation —
lives in the gRPC servicer + AssetsRepo. This module only translates HTTP
to gRPC and gRPC errors to HTTP statuses, per the spec's "thin gateway"
principle.
"""

from __future__ import annotations

import grpc
from fastapi import APIRouter, Depends, HTTPException, Query, status

from .._responses import CATALOG_CREATE_RESPONSES, CATALOG_LIST_RESPONSES
from .._timeframes import rest_to_storage
from ..auth import require_bearer, require_bearer_admin
from ..deps import get_grpc_client
from ..grpc_client import AssetRow as ClientAssetRow
from ..grpc_client import AssetWithStatusRow, BarServiceClient
from ..models import (
    Asset,
    AssetClass,
    AssetStatus,
    AssetWithStatus,
    CreateAssetRequest,
    CreateAssetResponse,
    ListAssetsResponse,
)

router = APIRouter(prefix="/v1", tags=["catalog"])


def _row_to_asset_model(r: ClientAssetRow) -> Asset:
    return Asset(
        symbol=r.symbol,
        name=r.name,
        exchange=r.exchange,
        # `type` mirrors AssetClass lowercased — same convention as
        # /v1/search and /v1/profile keep the response shape consistent.
        type=r.asset_class.lower() if r.asset_class else None,
        currency=r.currency,
        asset_class=AssetClass(r.asset_class),
        asset_sub_class=r.asset_subclass,
        isin=r.isin,
        country=r.country,
    )


def _row_to_status_model(r: AssetWithStatusRow) -> AssetWithStatus:
    return AssetWithStatus(
        asset=_row_to_asset_model(r.asset),
        status=AssetStatus(r.status),
        added_at=r.added_at,
        last_bar_ts=r.last_bar_ts,
    )


def _grpc_unavailable(exc: grpc.RpcError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail={
            "error": "grpc_unavailable",
            "message": (
                f"AssetService unreachable: "
                f"{exc.code().name if exc.code() else 'UNKNOWN'}"
            ),
        },
    )


@router.get(
    "/assets",
    response_model=ListAssetsResponse,
    response_model_exclude_none=True,
    operation_id="listAssets",
    summary="List the curated asset catalog with per-asset polling status.",
    responses=CATALOG_LIST_RESPONSES,
    dependencies=[Depends(require_bearer)],
)
def list_assets(
    exchange: str | None = Query(None, max_length=64),
    asset_class: AssetClass | None = Query(None),
    q: str | None = Query(None, min_length=1, max_length=64),
    cursor: str | None = Query(None, max_length=32),
    limit: int = Query(100, ge=1, le=500),
    grpc_client: BarServiceClient = Depends(get_grpc_client),
) -> ListAssetsResponse:
    try:
        rows, next_cursor = grpc_client.list_assets(
            exchange=exchange,
            asset_class=asset_class.value if asset_class else None,
            q=q,
            cursor=cursor,
            limit=limit,
        )
    except grpc.RpcError as exc:
        raise _grpc_unavailable(exc) from exc

    return ListAssetsResponse(
        assets=[_row_to_status_model(r) for r in rows],
        next_cursor=next_cursor,
    )


@router.post(
    "/assets",
    response_model=CreateAssetResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
    operation_id="createAsset",
    summary="Onboard a new asset and start polling its feeds.",
    responses=CATALOG_CREATE_RESPONSES,
    dependencies=[Depends(require_bearer_admin)],
)
def create_asset(
    body: CreateAssetRequest,
    grpc_client: BarServiceClient = Depends(get_grpc_client),
) -> CreateAssetResponse:
    asset_to_create = ClientAssetRow(
        symbol=body.symbol,
        storage_symbol=body.storage_symbol,
        name=body.name,
        exchange=body.exchange,
        currency=body.currency,
        asset_class=body.asset_class.value,
        asset_subclass=body.asset_sub_class,
        isin=body.isin,
        country=body.country,
    )
    # Pydantic returns Timeframe enums in `timeframes`; the servicer (and
    # `feeds.timeframe` storage column) expect the storage-side string
    # form, so translate REST → storage on the boundary (see
    # data_svc/rest/_timeframes.py — PR #11). Empty list → servicer
    # default (["1h"]).
    timeframes = [rest_to_storage(tf.value) for tf in (body.timeframes or [])]

    try:
        row, created, poll_eta = grpc_client.create_asset(
            asset=asset_to_create,
            timeframes=timeframes,
            tv_symbol=body.tv_symbol,
        )
    except grpc.RpcError as exc:
        if exc.code() == grpc.StatusCode.INVALID_ARGUMENT:
            # Defence in depth — Pydantic should have already caught this
            # via the schema; servicer-side validation is the canonical
            # check.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "error": "invalid_argument",
                    "message": exc.details() or "request rejected by AssetService",
                },
            ) from exc
        raise _grpc_unavailable(exc) from exc

    if not created:
        # Surface as 409 with the existing record. Matches the openapi
        # AlreadyExistsResponse schema.
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "already_exists",
                "message": (
                    f"{row.asset.symbol} already exists in the catalog."
                ),
                # Pydantic model dump — by_alias so the JSON keys match the
                # AlreadyExistsResponse schema (`addedAt`, `lastBarTs`).
                # mode="json" so enums (AssetClass, AssetStatus) become
                # their string values rather than Enum members — the
                # default exception handler json.dumps() the dict and
                # can't serialize Enum instances.
                "existing": _row_to_status_model(row).model_dump(
                    mode="json", by_alias=True, exclude_none=True,
                ),
            },
        )

    return CreateAssetResponse(
        asset=_row_to_status_model(row),
        created=created,
        poll_eta_seconds=poll_eta,
    )
