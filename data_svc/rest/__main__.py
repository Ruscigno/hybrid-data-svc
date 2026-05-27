"""REST gateway entry point.

    python -m data_svc.rest

Reads bars + assets from Postgres and serves them over HTTP. Single
process; uvicorn's default single-worker mode is enough for the
intended consumer (Ghostfolio's hourly cron).
"""

from __future__ import annotations

import logging

import uvicorn

from .settings import RestSettings


def _setup_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def main() -> None:
    _setup_logging()
    settings = RestSettings()
    uvicorn.run(
        "data_svc.rest.app:create_app",
        factory=True,
        host=settings.rest_listen_host,
        port=settings.rest_listen_port,
        log_config=None,  # reuse the root logger config above
    )


if __name__ == "__main__":
    main()
