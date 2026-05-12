"""
gRPC server entry point.

    python -m data_svc.grpc_server

Reads bars from Postgres and serves them over gRPC. Single-threaded read-only
path — no TradingView calls, no writes. The writer is `python -m data_svc`.
"""
from __future__ import annotations

import logging
import os
import signal
import time
from concurrent import futures

import grpc

from .proto import bars_pb2_grpc as _pb_grpc
from .service import build_servicer


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


logger = logging.getLogger(__name__)


def main() -> None:
    _setup_logging()

    listen_addr = os.getenv("GRPC_LISTEN", "0.0.0.0:50051")
    workers = int(os.getenv("GRPC_WORKERS", "10"))

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=workers))
    _pb_grpc.add_BarServiceServicer_to_server(build_servicer(), server)
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
