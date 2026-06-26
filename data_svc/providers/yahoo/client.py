"""Yahoo Finance HTTP client.

Uses ``curl_cffi`` browser TLS impersonation to avoid 429s from Yahoo's edge.
The ``get`` parameter is injectable for pure unit tests (no network required).
"""

from __future__ import annotations

import pandas as pd

from .parse import parse_chart
from .ratelimit import AdaptiveRateLimiter


class YahooThrottled(Exception):
    """Raised when Yahoo returns 429 or 999 (soft-block)."""


class YahooClient:
    """Fetches OHLCV data from Yahoo Finance v8 chart endpoint.

    Parameters
    ----------
    limiter:        :class:`AdaptiveRateLimiter` controlling request pacing.
    impersonate:    curl_cffi browser profile (default ``"chrome"``).
    read_timeout:   HTTP read timeout in seconds.
    get:            Injectable callable ``(url: str, params: dict) -> _Resp``.
                    If ``None``, a ``curl_cffi.requests.Session`` is created.
    """

    HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")

    def __init__(
        self,
        limiter: AdaptiveRateLimiter,
        *,
        impersonate: str = "chrome",
        read_timeout: float = 15.0,
        get=None,
    ) -> None:
        self._limiter = limiter
        self._impersonate = impersonate
        self._read_timeout = read_timeout

        if get is not None:
            self._get = get
        else:
            # Lazy import so the module can be imported without curl_cffi installed
            # (tests inject a fake getter and never trigger this branch).
            from curl_cffi.requests import Session  # type: ignore[import-untyped]

            session = Session(impersonate=impersonate)
            self._get = lambda url, params: session.get(
                url, params=params, timeout=read_timeout
            )

    def fetch_chart(
        self,
        symbol: str,
        interval: str,
        *,
        period: str | None = None,
        start: int | None = None,
        end: int | None = None,
    ) -> pd.DataFrame:
        """Fetch OHLCV bars from Yahoo Finance.

        Paces via the limiter. On 429/999 calls ``limiter.on_429()`` and raises
        :exc:`YahooThrottled`. On 200 calls ``limiter.on_success()`` and returns
        the parsed DataFrame. Any other status / network error is re-raised.
        """
        host = self.HOSTS[0]
        url = f"https://{host}/v8/finance/chart/{symbol}"

        params: dict = {"interval": interval}
        if start is not None and end is not None:
            params["period1"] = start
            params["period2"] = end
        elif period is not None:
            params["range"] = period

        self._limiter.acquire()

        resp = self._get(url, params)

        if resp.status_code in (429, 999):
            self._limiter.on_429()
            raise YahooThrottled(
                f"Yahoo returned {resp.status_code} for {symbol}"
            )

        if resp.status_code == 200:
            self._limiter.on_success()
            return parse_chart(resp.json())

        # Other status codes (4xx, 5xx) — propagate without calling on_success
        resp_text = getattr(resp, "text", "")
        raise RuntimeError(
            f"Yahoo chart request failed: HTTP {resp.status_code} for {symbol}: {resp_text}"
        )
