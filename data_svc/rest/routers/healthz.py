"""/healthz — liveness probe. Always open (no auth).

Per spec §"GET /healthz": returns 200 if the gateway can reach the gRPC
service and the Postgres connection is alive. Body shape is exactly
`{status, grpc_reachable, db_reachable}`.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..deps import get_bar_cache, get_grpc_client
from ..grpc_client import BarServiceClient
from ..models import HealthResponse, Status
from ...db.cache import BarCache

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
    bar_cache: BarCache = Depends(get_bar_cache),
    grpc_client: BarServiceClient = Depends(get_grpc_client),
) -> HealthResponse:
    db_reachable = False
    try:
        with bar_cache._pool.connection() as conn:  # noqa: SLF001 (pool is internal)
            conn.execute("SELECT 1").fetchone()
        db_reachable = True
    except Exception as exc:
        logger.warning("healthz: db unreachable: %s", exc)

    grpc_reachable = grpc_client.ping()

    status_val = Status.ok if (db_reachable and grpc_reachable) else Status.degraded
    return HealthResponse(
        status=status_val,
        grpc_reachable=grpc_reachable,
        db_reachable=db_reachable,
    )
