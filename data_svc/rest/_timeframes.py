"""Translate REST-surface timeframe values to the storage form used by the
gRPC service / Postgres `bars.timeframe` column.

The REST contract (docs/openapi.yaml) advertises timeframes with the `m`
suffix for minute granularities (`1m`, `3m`, `5m`, `15m`, `30m`). The
internal stack (data-svc writer, BarService, `bars.timeframe`) inherited
the TradingView CLI convention which omits the `m` (`15`, `30`). Bare
strings like `1h`, `4h`, `1D`, `1W` are identical on both sides.

REST routes must translate REST→storage before calling BarService gRPC,
otherwise queries for `15m` / `30m` return 503 no_data even when bars
exist (verified empirically post-deploy of PR #10).
"""

from __future__ import annotations

# REST surface (key) → storage form on the gRPC / DB side (value).
# Entries omitted here are identity-mapped (REST value == storage value).
_REST_TO_STORAGE = {
    "1m": "1",
    "3m": "3",
    "5m": "5",
    "15m": "15",
    "30m": "30",
}


def rest_to_storage(rest_timeframe: str) -> str:
    """Return the storage-side timeframe string for a REST input.

    Idempotent on values that don't need translation (`1h`, `4h`, `1D`, `1W`).
    """
    return _REST_TO_STORAGE.get(rest_timeframe, rest_timeframe)
