"""/healthz — liveness probe. Always open (no auth).

Per spec §"GET /healthz": returns 200 if the gateway can reach the gRPC
service and the Postgres connection is alive. Body shape is exactly
`{status, grpc_reachable, db_reachable}`.

Implementation: single round-trip to BarService.Ping. The gRPC server
runs a SELECT 1 inside Ping and returns db_reachable. If the RPC itself
fails, grpc_reachable=False (and db_reachable=False since we can't ask).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends

from ..deps import get_grpc_client
from ..grpc_client import BarServiceClient
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
    grpc_client: BarServiceClient = Depends(get_grpc_client),
) -> HealthResponse:
    grpc_reachable, db_reachable = grpc_client.ping()
    status_val = Status.ok if (grpc_reachable and db_reachable) else Status.degraded
    return HealthResponse(
        status=status_val,
        grpc_reachable=grpc_reachable,
        db_reachable=db_reachable,
    )
