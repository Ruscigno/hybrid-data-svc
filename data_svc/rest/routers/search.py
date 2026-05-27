"""/v1/search — substring search over the asset catalog."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from .._responses import SEARCH_RESPONSES
from ..auth import require_bearer
from ..deps import get_assets_repo
from ..models import Asset, AssetClass, SearchResponse
from ...db.assets import AssetRow, AssetsRepo

router = APIRouter(prefix="/v1", tags=["search"], dependencies=[Depends(require_bearer)])


def _row_to_model(r: AssetRow) -> Asset:
    return Asset(
        symbol=r.symbol,
        name=r.name,
        exchange=r.exchange,
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
    assets: AssetsRepo = Depends(get_assets_repo),
) -> SearchResponse:
    rows = assets.search(q, limit)
    return SearchResponse(results=[_row_to_model(r) for r in rows])
