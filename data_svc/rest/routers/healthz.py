"""/healthz — liveness probe. No auth.

Reports DB reachability and whether the asset catalog has been loaded.
Two booleans are checked independently so an operator can distinguish
"DB is down" from "we haven't seeded assets yet".
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..deps import get_assets_repo, get_bar_cache
from ..models import HealthResponse, Status

router = APIRouter(tags=["health"])
logger = logging.getLogger(__name__)


@router.get(
    "/healthz",
    response_model=HealthResponse,
    response_model_exclude_none=True,
    operation_id="getHealthz",
    summary="Liveness probe.",
)
def get_healthz(
    bar_cache=Depends(get_bar_cache),
    assets=Depends(get_assets_repo),
) -> HealthResponse:
    db_reachable = False
    assets_loaded = False
    try:
        with bar_cache._pool.connection() as conn:  # noqa: SLF001 (pool is internal)
            conn.execute("SELECT 1").fetchone()
        db_reachable = True
    except Exception as exc:
        logger.warning("healthz: db unreachable: %s", exc)

    if db_reachable:
        try:
            assets_loaded = assets.count() > 0
        except Exception as exc:
            logger.warning("healthz: assets count failed: %s", exc)

    status_val = Status.ok if (db_reachable and assets_loaded) else Status.degraded
    return HealthResponse(
        status=status_val,
        db_reachable=db_reachable,
        assets_loaded=assets_loaded,
    )
