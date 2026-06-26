"""Yahoo Finance HTTP client.

Uses ``curl_cffi`` browser TLS impersonation to avoid 429s from Yahoo's edge.
The ``get`` parameter is injectable for pure unit tests (no network required).
"""

from __future__ import annotations

from email.utils import parsedate_to_datetime
from typing import Callable
from urllib.parse import quote

import pandas as pd

from .parse import parse_chart
from .ratelimit import AdaptiveRateLimiter

#: Default fallback when Retry-After is present but unparseable as an integer.
_RETRY_AFTER_DEFAULT = 60.0


class YahooThrottled(Exception):
    """Raised when Yahoo returns 429 or 999 (soft-block).

    Attributes
    ----------
    retry_after:
        Cooldown duration in seconds parsed from the ``Retry-After`` response
        header, or ``None`` when the header was absent.
    """

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after: float | None = retry_after


def _parse_retry_after(value: str) -> float:
    """Parse a ``Retry-After`` header value into seconds.

    Handles integer seconds and HTTP-date formats. Returns
    ``_RETRY_AFTER_DEFAULT`` for any value that cannot be parsed.
    """
    value = value.strip()
    try:
        return float(int(value))
    except ValueError:
        pass
    try:
        dt = parsedate_to_datetime(value)
        import time as _time
        remaining = dt.timestamp() - _time.time()
        return max(0.0, remaining)
    except Exception:
        return _RETRY_AFTER_DEFAULT


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
        get: Callable[[str, dict], object] | None = None,
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
        encoded_symbol = quote(symbol, safe="")
        params: dict = {"interval": interval}
        if start is not None and end is not None:
            params["period1"] = start
            params["period2"] = end
        elif period is not None:
            params["range"] = period

        self._limiter.acquire()

        last_exc: Exception | None = None
        resp = None
        for host in self.HOSTS:
            url = f"https://{host}/v8/finance/chart/{encoded_symbol}"
            try:
                resp = self._get(url, params)
                break  # host answered — do not failover on HTTP status
            except Exception as exc:  # network/connection error
                last_exc = exc
                resp = None

        if resp is None:
            # All hosts raised a network error
            assert last_exc is not None
            raise last_exc

        if resp.status_code in (429, 999):
            ra_header = (resp.headers or {}).get("Retry-After")
            retry_after: float | None = None
            if ra_header is not None:
                retry_after = _parse_retry_after(ra_header)
                self._limiter.notify_retry_after(retry_after)
            self._limiter.on_429()
            raise YahooThrottled(
                f"Yahoo returned {resp.status_code} for {symbol}",
                retry_after=retry_after,
            )

        if resp.status_code == 200:
            self._limiter.on_success()
            return parse_chart(resp.json())

        # Other status codes (4xx, 5xx) — propagate without calling on_success
        resp_text = getattr(resp, "text", "")[:200]
        raise RuntimeError(
            f"Yahoo chart request failed: HTTP {resp.status_code} for {symbol}: {resp_text}"
        )
