"""
TradingView CLI wrapper for the data service.

_tv_switch  — switches the active chart to a given TV symbol/timeframe
_tv_fetch   — calls `tv ohlcv -n N` and returns a DataFrame
DataFetcher — wraps _tv_fetch with the shared BarCache; skips TV when cache is fresh
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
import time
from typing import Optional

import pandas as pd

from .config import DataSvcConfig, Feed
from .db.cache import BarCache, _is_plausible
from .tab_pin import TabPin, TabPinError

logger = logging.getLogger(__name__)


class DataFetchError(Exception):
    pass


_TF_TO_TV: dict[str, str] = {
    "1": "1", "3": "3", "5": "5", "15": "15", "30": "30",
    "60": "60", "1h": "60", "2h": "120", "4h": "240",
    "D": "D", "1D": "D", "W": "W", "1W": "W",
}

_TF_SECONDS: dict[str, int] = {
    "1": 60, "3": 180, "5": 300, "15": 900, "30": 1800,
    "60": 3600, "1h": 3600, "2h": 7200, "4h": 14400,
    "D": 86400, "1D": 86400, "W": 604800, "1W": 604800,
}


def bar_secs(timeframe: str) -> int:
    s = _TF_SECONDS.get(timeframe)
    if s is None:
        raise ValueError(f"Unknown timeframe: {timeframe!r}")
    return s


def _tv_env(host: Optional[str], port: Optional[int]) -> Optional[dict]:
    """Build the env passed to the tv CLI subprocess.

    When (host, port) are known, set CDP_HOST/CDP_PORT explicitly so the Node
    tv CLI uses them directly instead of running its own discovery on every
    call (wasteful, and could race the data-svc parent).
    Returns None when both are unset — subprocess inherits process env as-is.
    """
    if host is None and port is None:
        return None
    env = dict(os.environ)
    if host is not None:
        env["CDP_HOST"] = str(host)
    if port is not None:
        env["CDP_PORT"] = str(port)
    return env


def _dump_tv_failure(context: str, result: subprocess.CompletedProcess) -> str:
    """Write the tv CLI's complete stdout+stderr to a temp file for post-mortem.

    Returns the dump path, or a marker string if the dump itself failed. The
    truncation/corruption that breaks the JSON parse sits well past
    stdout[:200], so the full capture is the only way to see the real cause.
    """
    stdout = result.stdout or ""
    stderr = result.stderr or ""
    path = os.path.join(
        tempfile.gettempdir(), f"tv_fail_{context}_{int(time.time())}.txt"
    )
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(f"# context={context} returncode={result.returncode}\n")
            fh.write(f"# --- stdout ({len(stdout)} bytes) ---\n")
            fh.write(stdout)
            fh.write(f"\n# --- stderr ({len(stderr)} bytes) ---\n")
            fh.write(stderr)
    except OSError as exc:
        logger.error("[tv:%s] failed to write diagnostic dump %s: %s",
                     context, path, exc)
        return f"<dump failed: {exc}>"
    return path


def _parse_tv_json(result: subprocess.CompletedProcess, context: str) -> dict:
    """Parse the tv CLI's stdout as JSON, with real diagnostics on failure.

    Uses raw_decode() so a valid JSON object followed by trailing junk still
    parses (defends against a stray line appended after the payload). A hard
    failure dumps the complete stdout/stderr to a file and raises a
    DataFetchError carrying the decode position, byte counts and a snippet
    around the error — enough to tell truncation apart from corruption.
    """
    text = (result.stdout or "").strip()
    if text:
        try:
            payload, end = json.JSONDecoder().raw_decode(text)
        except json.JSONDecodeError as exc:
            dump = _dump_tv_failure(context, result)
            snippet = text[max(0, exc.pos - 120):exc.pos + 120]
            raise DataFetchError(
                f"tv CLI returned non-JSON ({context}): {exc.msg} at "
                f"pos={exc.pos} line={exc.lineno} col={exc.colno}; "
                f"stdout={len(result.stdout or '')} bytes, "
                f"returncode={result.returncode}; "
                f"around-error={snippet!r}; full output dumped to {dump}"
            ) from exc
        if end < len(text):
            logger.warning(
                "[tv:%s] ignored %d trailing byte(s) after JSON payload",
                context, len(text) - end,
            )
        return payload

    dump = _dump_tv_failure(context, result)
    raise DataFetchError(
        f"tv CLI returned empty stdout ({context}); "
        f"returncode={result.returncode}; "
        f"stderr={(result.stderr or '').strip()[:300]!r}; "
        f"full output dumped to {dump}"
    )


def _tv_switch(tv_symbol: str, timeframe: str, tv_cli: str,
               tv_env: Optional[dict] = None) -> None:
    tf_tv = _TF_TO_TV.get(timeframe, timeframe)
    for subcmd, arg in [("symbol", tv_symbol), ("timeframe", tf_tv)]:
        try:
            result = subprocess.run(
                [tv_cli, subcmd, arg], capture_output=True, text=True,
                timeout=15, env=tv_env,
            )
        except subprocess.TimeoutExpired:
            raise DataFetchError(f"tv {subcmd} switch timed out for {arg!r}")
        if result.returncode != 0:
            raise DataFetchError(f"tv {subcmd} switch failed: {result.stderr.strip()}")
        data = _parse_tv_json(result, context=subcmd)
        if not data.get("success"):
            raise DataFetchError(f"tv {subcmd} returned failure: {data}")


def _tv_fetch(count: int, tv_cli: str, tv_env: Optional[dict] = None) -> pd.DataFrame:
    cmd = [tv_cli, "ohlcv", "-n", str(min(count, 500))]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, env=tv_env,
        )
    except subprocess.TimeoutExpired:
        raise DataFetchError(f"tv CLI timed out: {' '.join(cmd)}")
    except FileNotFoundError:
        raise DataFetchError(
            f"tv CLI not found at {tv_cli!r}. "
            "Ensure tradingview-mcp is installed and `tv` is on PATH."
        )

    if result.returncode != 0:
        raise DataFetchError(f"tv CLI failed (exit {result.returncode}): {result.stderr.strip()}")

    payload = _parse_tv_json(result, context="ohlcv")

    if not payload.get("success"):
        raise DataFetchError(f"tv CLI reported failure: {payload}")

    bars = payload.get("bars", [])
    if not bars:
        raise DataFetchError("tv CLI returned empty bars array")

    df = pd.DataFrame(bars)

    if "time" not in df.columns:
        raise DataFetchError(f"Unexpected bar schema — columns: {list(df.columns)}")

    if df["time"].iloc[0] > 1e12:
        df["time"] = (df["time"] // 1000).astype(int)
    else:
        df["time"] = df["time"].astype(int)

    missing = {"open", "high", "low", "close", "volume"} - set(df.columns)
    if missing:
        raise DataFetchError(f"Missing columns in bar data: {missing}")

    return df[["time", "open", "high", "low", "close", "volume"]].copy()


class DataFetcher:
    """
    Fetches bars from TradingView via the tv CLI and writes them to the shared cache.
    Single-threaded; chart switches are serialized by sequential loop calls.
    """

    def __init__(self, cfg: DataSvcConfig) -> None:
        self._cfg = cfg
        self._cache = BarCache(cfg.postgres_url)
        primary = cfg.feeds[0] if cfg.feeds else Feed(cfg.symbol, cfg.timeframe, cfg.tv_symbol)
        self._chart_tv_symbol: str = primary.tv_symbol
        self._chart_timeframe: str = primary.timeframe
        # Resolve CDP endpoint once at boot (env override or discovery), then
        # pass it to TabPin AND to every tv-CLI subprocess so the Node side
        # uses the same port without re-running discovery on each call.
        host, port = cfg.resolve_cdp()
        self._cdp_host: str = host
        self._cdp_port: int = port
        self._tv_env = _tv_env(host, port)
        self._tab_pin = TabPin(host=host, port=port)
        self._tab_pin.resolve()

    @property
    def cache(self) -> BarCache:
        return self._cache

    @property
    def tab_pin(self) -> TabPin:
        return self._tab_pin

    def fetch(self, feed: Feed) -> pd.DataFrame:
        secs = bar_secs(feed.timeframe)
        last_ts = self._cache.latest_bar_ts(feed.symbol, feed.timeframe)

        # Fast path: no new closed bar is possible yet.
        if last_ts is not None and (time.time() - last_ts) < 2 * secs:
            logger.debug("[%s/%s] cache fresh — serving from DB", feed.symbol, feed.timeframe)
            return self._cache.read_bars(feed.symbol, feed.timeframe, self._cfg.bars_to_fetch)

        # Slow path: ensure the pinned tab is in focus before any TV CLI call.
        try:
            self._tab_pin.activate()
        except TabPinError as exc:
            raise DataFetchError(f"tab pin activation failed: {exc}") from exc

        if feed.tv_symbol != self._chart_tv_symbol or feed.timeframe != self._chart_timeframe:
            logger.info("[%s/%s] switching chart → %s", feed.symbol, feed.timeframe, feed.tv_symbol)
            _tv_switch(feed.tv_symbol, feed.timeframe, self._cfg.tv_cli, tv_env=self._tv_env)
            self._chart_tv_symbol = feed.tv_symbol
            self._chart_timeframe = feed.timeframe
            time.sleep(3)

        def _fetcher(_sym: str, _tf: str, cnt: int) -> pd.DataFrame:
            df = _tv_fetch(cnt, self._cfg.tv_cli, tv_env=self._tv_env)
            # Verify-after-fetch: catch chart-switch race where tv ohlcv returns
            # bars from the pre-switch symbol. PLAUSIBLE_RANGES catches gross
            # mismatches; BarCache._insert_bars has additional drift + band guards.
            bad = [float(c) for c in df["close"] if not _is_plausible(_sym, float(c))]
            if bad:
                sample = bad[:3]
                logger.error(
                    "[%s/%s] tv_fetch returned %d/%d closes outside plausible "
                    "band (sample=%s) — chart switch race suspected, raising",
                    _sym, _tf, len(bad), len(df), sample,
                )
                raise DataFetchError(
                    f"tv_fetch returned implausible closes for {_sym} (sample={sample})"
                )
            return df

        return self._cache.get_bars(feed.symbol, feed.timeframe, self._cfg.bars_to_fetch, _fetcher)
