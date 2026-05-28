"""/v1/profile/{symbol} — thin gateway over AssetService.GetProfile."""

from __future__ import annotations

import grpc
from fastapi import APIRouter, Depends, HTTPException, Path, status

from .._responses import PROFILE_RESPONSES
from ..auth import require_bearer
from ..deps import get_grpc_client
from ..grpc_client import BarServiceClient
from ..models import Asset, AssetClass

router = APIRouter(prefix="/v1", tags=["profile"], dependencies=[Depends(require_bearer)])


@router.get(
    "/profile/{symbol}",
    response_model=Asset,
    response_model_exclude_none=True,
    operation_id="getProfile",
    summary="Asset metadata lookup.",
    responses=PROFILE_RESPONSES,
)
def get_profile(
    symbol: str = Path(
        ...,
        min_length=3,
        max_length=32,
        pattern=r"^[A-Z0-9]+:[A-Z0-9._/-]+$",
    ),
    grpc_client: BarServiceClient = Depends(get_grpc_client),
) -> Asset:
    try:
        row = grpc_client.get_asset_profile(symbol)
    except grpc.RpcError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "grpc_unavailable",
                "message": f"AssetService unreachable: {exc.code().name if exc.code() else 'UNKNOWN'}",
            },
        ) from exc

    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "unknown_symbol",
                "message": f"{symbol} is not in the configured feed list",
            },
        )
    return Asset(
        symbol=row.symbol,
        name=row.name,
        exchange=row.exchange,
        type=row.asset_class.lower() if row.asset_class else None,
        currency=row.currency,
        asset_class=AssetClass(row.asset_class),
        asset_sub_class=row.asset_subclass,
        isin=row.isin,
        country=row.country,
    )
