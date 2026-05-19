"""
Pin the data-svc to a single TradingView Desktop tab via CDP.

The `tv` CLI's connection layer always picks the FIRST chart target returned by
`/json/list`. With multiple tabs open, this is non-deterministic from the
data-svc's perspective: a user-initiated tab switch could redirect every
subsequent `tv symbol/timeframe/ohlcv` call to the wrong chart.

TabPin resolves the active chart tab once at startup (the tab the user has in
focus) and re-activates it via `/json/activate/<id>` before each TV call.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


class TabPinError(Exception):
    pass


_CHART_URL_RE = "tradingview.com/chart"


@dataclass(frozen=True)
class PinnedTab:
    id: str
    title: str
    url: str
    chart_id: Optional[str]


class TabPin:
    def __init__(
        self,
        host: Optional[str] = None,
        port: Optional[int] = None,
        timeout: float = 5.0,
    ) -> None:
        # host/port may be None — that opts into discovery via cdp_discover
        # at resolve() time (lazy, so unit tests don't need a live CDP).
        self._host = host
        self._port = port
        self._timeout = timeout
        self._pinned: Optional[PinnedTab] = None

    @property
    def pinned(self) -> Optional[PinnedTab]:
        return self._pinned

    def _ensure_endpoint(self) -> None:
        if self._host is not None and self._port is not None:
            return
        # Lazy import — keeps tab_pin importable without cdp_discover (e.g. in tests).
        from .cdp_discover import discover_cdp
        host, port = discover_cdp()
        self._host = host
        self._port = port

    def resolve(self) -> PinnedTab:
        """Pin to the currently-active chart tab (first chart target in z-order)."""
        self._ensure_endpoint()
        targets = self._list_targets()
        chart = next(
            (t for t in targets if t.get("type") == "page" and _CHART_URL_RE in t.get("url", "").lower()),
            None,
        )
        if chart is None:
            raise TabPinError(
                f"No TradingView chart tab found at {self._host}:{self._port}. "
                "Is TradingView Desktop running with --remote-debugging-port=9222?"
            )
        self._pinned = PinnedTab(
            id=chart["id"],
            title=chart.get("title", ""),
            url=chart.get("url", ""),
            chart_id=_extract_chart_id(chart.get("url", "")),
        )
        logger.info(
            "tab pinned: id=%s chart_id=%s title=%r",
            self._pinned.id, self._pinned.chart_id, self._pinned.title,
        )
        return self._pinned

    def activate(self) -> None:
        """Bring the pinned tab to focus before a TV call. Raises if the tab is gone."""
        if self._pinned is None:
            raise TabPinError("TabPin.activate() called before resolve()")
        # resolve() was already called, so host/port are already set; but
        # _ensure_endpoint is idempotent and cheap when they are set.
        self._ensure_endpoint()

        targets = self._list_targets()
        if not any(t.get("id") == self._pinned.id for t in targets):
            raise TabPinError(
                f"Pinned tab {self._pinned.id!r} (title={self._pinned.title!r}) is no longer "
                "open. Reopen the chart tab and restart data-svc."
            )

        url = f"http://{self._host}:{self._port}/json/activate/{self._pinned.id}"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace").strip()
        except urllib.error.URLError as exc:
            raise TabPinError(f"CDP activate failed: {exc}") from exc

        if body and body != "Target activated":
            raise TabPinError(f"CDP activate returned unexpected body: {body!r}")

    def _list_targets(self) -> list[dict]:
        url = f"http://{self._host}:{self._port}/json/list"
        try:
            with urllib.request.urlopen(url, timeout=self._timeout) as resp:
                payload = resp.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as exc:
            raise TabPinError(f"CDP /json/list failed at {self._host}:{self._port}: {exc}") from exc
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TabPinError(f"CDP /json/list returned non-JSON: {payload[:200]}") from exc


def _extract_chart_id(url: str) -> Optional[str]:
    marker = "/chart/"
    idx = url.find(marker)
    if idx < 0:
        return None
    rest = url[idx + len(marker):]
    end = min((i for i in (rest.find("/"), rest.find("?")) if i >= 0), default=len(rest))
    chart_id = rest[:end]
    return chart_id or None
