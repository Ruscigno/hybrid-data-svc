"""Source ABC, source registry, and YahooSource concrete implementation.

D9: Source abstraction — downstream code depends on the Source interface,
not on a concrete provider.
"""

from __future__ import annotations

import abc
from typing import Any, Callable

import pandas as pd

from .ratelimit import AdaptiveRateLimiter
from .client import YahooClient


class Source(abc.ABC):
    """Abstract base for OHLCV data sources."""

    name: str

    @abc.abstractmethod
    def fetch(
        self,
        symbol: str,
        interval: str,
        *,
        period: str | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars. Returns an empty DataFrame on 'no data' — never raises."""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Any] = {}


def register_source(name: str, factory: Callable[..., "Source"]) -> None:
    """Register a source factory under ``name``."""
    _REGISTRY[name] = factory


def get_source(name: str, **kwargs) -> Source:
    """Construct and return a Source by registered name.

    Raises ``KeyError`` if the name is unknown.
    """
    if name not in _REGISTRY:
        raise KeyError(f"Unknown source {name!r}. Known: {sorted(_REGISTRY)}")
    return _REGISTRY[name](**kwargs)


# ---------------------------------------------------------------------------
# YahooSource
# ---------------------------------------------------------------------------

class YahooSource(Source):
    """Source implementation backed by :class:`~.client.YahooClient`."""

    name = "yahoo"

    def __init__(self, client: YahooClient) -> None:
        self._client = client

    def fetch(
        self,
        symbol: str,
        interval: str,
        *,
        period: str | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> pd.DataFrame:
        return self._client.fetch_chart(
            symbol, interval, period=period, start=start, end=end
        )


# ---------------------------------------------------------------------------
# Register "yahoo" at import time
# ---------------------------------------------------------------------------

def _yahoo_factory(
    rpm: float = 60.0,
    *,
    impersonate: str = "chrome",
    read_timeout: float = 15.0,
    get=None,
    limiter: AdaptiveRateLimiter | None = None,
    **_kw,
) -> YahooSource:
    """Factory for the 'yahoo' registry entry.

    Accepts either a pre-built ``limiter`` or a bare ``rpm``.
    ``get`` is injectable for tests.
    """
    if limiter is None:
        limiter = AdaptiveRateLimiter(rpm)
    client = YahooClient(
        limiter, impersonate=impersonate, read_timeout=read_timeout, get=get
    )
    return YahooSource(client)


register_source("yahoo", _yahoo_factory)
