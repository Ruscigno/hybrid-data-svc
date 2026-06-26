"""
gRPC server entry point.

    python -m data_svc.grpc_server

Reads bars + assets from Postgres and serves them over gRPC. Read-only
path — no TradingView calls, no writes (the writer is `python -m data_svc`).

Registers two services on the same port:
  - BarService: GetRecentBars, GetBarsInRange, HealthCheck, Ping
  - AssetService: GetProfile, Search (backs /v1/profile and /v1/search via
    the REST gateway)

At startup, runs the assets.yaml upsert so the catalog is ready before the
server accepts traffic.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from concurrent import futures
from pathlib import Path

import grpc

from ..db.assets import AssetsRepo
from ..db.postgres import assert_provider_schema
from ..services import assets_loader
from .assets_service import build_servicer as build_asset_servicer
from .proto import bars_pb2_grpc as _pb_grpc
from .service import build_servicer as build_bar_servicer


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


logger = logging.getLogger(__name__)


def _assets_yaml_path() -> Path:
    """Resolve the assets.yaml path; respects ASSETS_YAML_PATH env override
    (used by tests). Defaults to the file shipped alongside the package."""
    override = os.getenv("ASSETS_YAML_PATH")
    if override:
        return Path(override)
    return Path(__file__).resolve().parent.parent / "assets.yaml"


def main() -> None:
    _setup_logging()

    listen_addr = os.getenv("GRPC_LISTEN", "0.0.0.0:50051")
    workers = int(os.getenv("GRPC_WORKERS", "10"))
    pg_url = os.getenv(
        "POSTGRES_URL",
        "postgresql://datasvc:datasvc@postgres:5432/datasvc",
    )

    assert_provider_schema(pg_url)

    # Load the curated asset catalog before opening the port — REST routes
    # depend on the assets table being populated. Idempotent upsert.
    try:
        repo = AssetsRepo(pg_url)
        n = assets_loader.sync(repo, _assets_yaml_path(), strict=False)
        logger.info("[grpc] asset catalog loaded (%d row(s))", n)
    except Exception as exc:
        logger.error("[grpc] assets_loader failed at startup: %s", exc)

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    _pb_grpc.add_BarServiceServicer_to_server(build_bar_servicer(), server)
    _pb_grpc.add_AssetServiceServicer_to_server(build_asset_servicer(pg_url), server)
    server.add_insecure_port(listen_addr)
    server.start()

    logger.info("bar-grpc listening on %s (workers=%d)", listen_addr, workers)

    stop = {"flag": False}

    def _handle_signal(signum, _frame):  # noqa: ANN001
        logger.info("received signal %d — shutting down", signum)
        stop["flag"] = True

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    try:
        while not stop["flag"]:
            time.sleep(1.0)
    finally:
        server.stop(grace=5.0)
        logger.info("bar-grpc stopped")


if __name__ == "__main__":
    main()
