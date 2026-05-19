"""
Intelligent CDP port discovery for the TradingView Desktop integration.

Why: TradingView Desktop's --remote-debugging-port is not always 9222. Other
Chromium-based apps (Antigravity, Chrome, dev tools) may grab 9222 first,
pushing TV to 9223+. Hardcoding any single port makes the stack brittle.

Algorithm:
  1. If env CDP_PORT is set, use it verbatim (operator escape hatch).
  2. Probe the cached port (if a cache file exists). If the host:port still
     hosts a TV instance, return it (fast path — no scan).
  3. Scan the configured port range. The first port whose `/json/list`
     contains a tab matching tradingview.com/chart wins. Fallback: any
     tab URL containing "tradingview" (case-insensitive).
  4. Write the chosen endpoint to the cache file atomically.

The same algorithm and cache file format are implemented in
external/tradingview-mcp/src/cdp_discover.js so the Node MCP server and
the Python data-svc converge on the same port without coordination.

Cache file:
  Path:   ${TV_MCP_CACHE_FILE} || ${XDG_CACHE_HOME or ~/.cache}/tv-mcp/cdp-port.json
  Schema: {"host": str, "port": int, "discovered_at": ISO-8601, "probe_url": str}

Env overrides:
  CDP_PORT             explicit port (skip discovery)
  CDP_HOST             host (defaults to "localhost")
  TV_MCP_CACHE_FILE    cache file path override
  TV_MCP_PORT_RANGE    port range "low-high" (default "9222-9230")
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_HOST = "localhost"
_DEFAULT_PORT_RANGE = (9222, 9230)
_PROBE_TIMEOUT_S = 0.5  # short — we may probe several ports back-to-back

_TV_CHART_PATTERN = re.compile(r"tradingview\.com/chart", re.IGNORECASE)
_TV_GENERIC_PATTERN = re.compile(r"tradingview", re.IGNORECASE)


class DiscoveryError(RuntimeError):
    """Raised when no TradingView CDP endpoint can be found."""


# ---------------------------------------------------------------------------
# Cache file helpers
# ---------------------------------------------------------------------------

def _cache_file_path() -> Path:
    override = os.environ.get("TV_MCP_CACHE_FILE")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache"
    )
    return Path(base) / "tv-mcp" / "cdp-port.json"


def _read_cache() -> Optional[Tuple[str, int]]:
    path = _cache_file_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        host = str(data["host"])
        port = int(data["port"])
        return host, port
    except (json.JSONDecodeError, KeyError, ValueError, OSError) as exc:
        logger.debug("[cdp_discover] cache read failed: %s", exc)
        return None


def _write_cache(host: str, port: int) -> None:
    path = _cache_file_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("[cdp_discover] cannot create cache dir %s: %s", path.parent, exc)
        return

    payload = {
        "host": host,
        "port": port,
        "discovered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "probe_url": f"http://{host}:{port}/json/version",
    }
    # Atomic write: write to a sibling tmp file, then rename.
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent), prefix=".tmp_", suffix=".json", delete=False
        ) as tf:
            json.dump(payload, tf)
            tmp_name = tf.name
        os.replace(tmp_name, str(path))
    except OSError as exc:
        logger.warning("[cdp_discover] cannot write cache file %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Port probing
# ---------------------------------------------------------------------------

def _http_get_json(url: str, timeout: float = _PROBE_TIMEOUT_S) -> Optional[object]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", errors="replace"))
    except (urllib.error.URLError, socket.timeout, json.JSONDecodeError, OSError):
        return None


def _is_tv_endpoint(host: str, port: int) -> bool:
    """True iff `host:port` exposes CDP with at least one TradingView tab open."""
    if _http_get_json(f"http://{host}:{port}/json/version") is None:
        return False
    targets = _http_get_json(f"http://{host}:{port}/json/list")
    if not isinstance(targets, list):
        return False
    for t in targets:
        if not isinstance(t, dict) or t.get("type") != "page":
            continue
        url = t.get("url", "") or ""
        if _TV_GENERIC_PATTERN.search(url):
            return True
    return False


def _parse_port_range(spec: Optional[str]) -> Tuple[int, int]:
    if not spec:
        return _DEFAULT_PORT_RANGE
    try:
        lo_s, hi_s = spec.split("-", 1)
        lo, hi = int(lo_s), int(hi_s)
        if 1 <= lo <= hi <= 65535:
            return lo, hi
    except (ValueError, AttributeError):
        pass
    logger.warning("[cdp_discover] invalid TV_MCP_PORT_RANGE=%r — using default", spec)
    return _DEFAULT_PORT_RANGE


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover_cdp(force: bool = False) -> Tuple[str, int]:
    """
    Resolve (host, port) for the TradingView CDP endpoint.

    `force=True` skips the cache (useful after a probe failure mid-session
    to force a rescan; env CDP_PORT still wins).
    """
    # 1. Explicit env override.
    env_port = os.environ.get("CDP_PORT")
    if env_port:
        host = os.environ.get("CDP_HOST", _DEFAULT_HOST)
        try:
            return host, int(env_port)
        except ValueError:
            logger.warning("[cdp_discover] invalid CDP_PORT=%r — falling back to discovery", env_port)

    host = os.environ.get("CDP_HOST", _DEFAULT_HOST)

    # 2. Fast path: validate the cached port.
    if not force:
        cached = _read_cache()
        if cached:
            c_host, c_port = cached
            # Honor env CDP_HOST if it changed since last cache write.
            probe_host = host if host != _DEFAULT_HOST else c_host
            if _is_tv_endpoint(probe_host, c_port):
                logger.info("[cdp_discover] cache hit: %s:%d", probe_host, c_port)
                return probe_host, c_port
            logger.info("[cdp_discover] cache miss at %s:%d — scanning", probe_host, c_port)

    # 3. Scan range.
    lo, hi = _parse_port_range(os.environ.get("TV_MCP_PORT_RANGE"))
    matches: list[int] = []
    for port in range(lo, hi + 1):
        if _is_tv_endpoint(host, port):
            matches.append(port)

    if not matches:
        raise DiscoveryError(
            f"No TradingView CDP endpoint found at {host}:{lo}..{hi}. "
            "Ensure TradingView Desktop is running with --remote-debugging-port=<PORT> "
            "AND at least one chart tab is open. To bypass discovery, set CDP_PORT explicitly."
        )

    # Prefer the cached port if it's still among the matches (port stability
    # across reboots). Otherwise pick the lowest.
    cached = _read_cache() if not force else None
    chosen: int
    if cached and cached[1] in matches:
        chosen = cached[1]
    else:
        chosen = matches[0]

    if len(matches) > 1:
        logger.warning(
            "[cdp_discover] %d TradingView endpoints found at %s (ports=%s); using %d",
            len(matches), host, matches, chosen,
        )
    else:
        logger.info("[cdp_discover] scanned %d-%d, found TV on %s:%d", lo, hi, host, chosen)

    _write_cache(host, chosen)
    return host, chosen
