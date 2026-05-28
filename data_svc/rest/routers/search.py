"""/v1/search — thin gateway over AssetService.Search."""

from __future__ import annotations

import grpc
from fastapi import APIRouter, Depends, HTTPException, Query, status

from .._responses import SEARCH_RESPONSES
from ..auth import require_bearer
from ..deps import get_grpc_client
from ..grpc_client import AssetRow, BarServiceClient
from ..models import Asset, AssetClass, SearchResponse

router = APIRouter(prefix="/v1", tags=["search"], dependencies=[Depends(require_bearer)])


def _row_to_model(r: AssetRow) -> Asset:
    return Asset(
        symbol=r.symbol,
        name=r.name,
        exchange=r.exchange,
        # `type` is the spec-shaped short label (lowercase) used in the
        # /v1/search example payload. Derived from asset_class.
        type=r.asset_class.lower() if r.asset_class else None,
        currency=r.currency,
        asset_class=AssetClass(r.asset_class),
        asset_sub_class=r.asset_subclass,
        isin=r.isin,
        country=r.country,
    )


@router.get(
    "/search",
    response_model=SearchResponse,
    response_model_exclude_none=True,
    operation_id="search",
    summary="Substring search over the asset catalog.",
    responses=SEARCH_RESPONSES,
)
def search(
    q: str = Query(..., min_length=1, max_length=64),
    limit: int = Query(10, ge=1, le=50),
    grpc_client: BarServiceClient = Depends(get_grpc_client),
) -> SearchResponse:
    try:
        rows = grpc_client.search_assets(q, limit)
    except grpc.RpcError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "grpc_unavailable",
                "message": f"AssetService unreachable: {exc.code().name if exc.code() else 'UNKNOWN'}",
            },
        ) from exc

    return SearchResponse(results=[_row_to_model(r) for r in rows])
