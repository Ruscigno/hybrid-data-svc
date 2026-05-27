"""
Tests for data_svc.cdp_discover.

Strategy: spin up a tiny HTTP server on a random localhost port that mimics
a Chromium /json/version + /json/list endpoint. Vary the responses to simulate:
  - a TV instance (chart tab present)
  - a non-TV Chromium (no tradingview URL)
  - a dead port (server stopped)
Then assert the discovery algorithm picks the right one.
"""

from __future__ import annotations

import http.server
import json
import os
import socket
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

import pytest

from data_svc import cdp_discover


# ---------------------------------------------------------------------------
# Fake CDP server
# ---------------------------------------------------------------------------

class _FakeCdpHandler(http.server.BaseHTTPRequestHandler):
    """Serves /json/version + /json/list with configurable responses."""

    tabs: list[dict] = []  # class-level so tests can mutate per-instance

    def do_GET(self):  # noqa: N802
        if self.path == "/json/version":
            self._send_json({"Browser": "Chrome/test", "Protocol-Version": "1.3"})
        elif self.path == "/json/list":
            self._send_json(self.__class__.tabs)
        else:
            self.send_error(404)

    def _send_json(self, body):
        payload = json.dumps(body).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args, **kwargs):  # silence stderr noise
        pass


@contextmanager
def fake_cdp(tabs: list[dict]) -> Iterator[int]:
    """Start a fake CDP server on an ephemeral port. Returns the port."""
    handler_cls = type("_H", (_FakeCdpHandler,), {"tabs": tabs})
    httpd = http.server.HTTPServer(("127.0.0.1", 0), handler_cls)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    try:
        yield port
    finally:
        httpd.shutdown()
        httpd.server_close()
        t.join(timeout=2)


def tv_tabs() -> list[dict]:
    return [{"type": "page", "url": "https://www.tradingview.com/chart/abc123/", "id": "T"}]


def non_tv_tabs() -> list[dict]:
    return [{"type": "page", "url": "https://example.com/", "id": "X"}]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_env_and_cache(tmp_path, monkeypatch):
    """Isolate each test: ephemeral cache file, no CDP_* env leaks."""
    cache_file = tmp_path / "cdp-port.json"
    monkeypatch.setenv("TV_MCP_CACHE_FILE", str(cache_file))
    monkeypatch.delenv("CDP_PORT", raising=False)
    monkeypatch.delenv("CDP_HOST", raising=False)
    monkeypatch.delenv("TV_MCP_PORT_RANGE", raising=False)
    yield cache_file


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_env_override_skips_discovery(monkeypatch, _clean_env_and_cache):
    monkeypatch.setenv("CDP_PORT", "9999")
    host, port = cdp_discover.discover_cdp()
    assert (host, port) == ("localhost", 9999)
    # cache should NOT be written when env override wins
    assert not _clean_env_and_cache.exists()


def test_scan_finds_tv_on_only_match(monkeypatch, _clean_env_and_cache):
    with fake_cdp(tv_tabs()) as port:
        monkeypatch.setenv("TV_MCP_PORT_RANGE", f"{port}-{port}")
        monkeypatch.setenv("CDP_HOST", "127.0.0.1")
        host, p = cdp_discover.discover_cdp()
        assert (host, p) == ("127.0.0.1", port)
        assert _clean_env_and_cache.exists()
        data = json.loads(_clean_env_and_cache.read_text())
        assert data["port"] == port


def test_cache_hit_skips_scan(monkeypatch, _clean_env_and_cache):
    with fake_cdp(tv_tabs()) as port:
        # Pre-warm the cache file.
        _clean_env_and_cache.parent.mkdir(parents=True, exist_ok=True)
        _clean_env_and_cache.write_text(json.dumps(
            {"host": "127.0.0.1", "port": port,
             "discovered_at": "2026-01-01T00:00:00Z", "probe_url": ""}
        ))
        monkeypatch.setenv("CDP_HOST", "127.0.0.1")
        # Make the scan range INVALID so we know it's hitting the cache.
        monkeypatch.setenv("TV_MCP_PORT_RANGE", "65000-65001")
        host, p = cdp_discover.discover_cdp()
        assert (host, p) == ("127.0.0.1", port)


def test_cache_miss_falls_back_to_scan(monkeypatch, _clean_env_and_cache):
    with fake_cdp(tv_tabs()) as port:
        # Cache points at a dead port.
        _clean_env_and_cache.parent.mkdir(parents=True, exist_ok=True)
        _clean_env_and_cache.write_text(json.dumps(
            {"host": "127.0.0.1", "port": 1,
             "discovered_at": "2026-01-01T00:00:00Z", "probe_url": ""}
        ))
        monkeypatch.setenv("CDP_HOST", "127.0.0.1")
        monkeypatch.setenv("TV_MCP_PORT_RANGE", f"{port}-{port}")
        host, p = cdp_discover.discover_cdp()
        assert (host, p) == ("127.0.0.1", port)


def test_non_tv_chromium_is_ignored(monkeypatch, _clean_env_and_cache):
    """A Chromium responding to CDP but without a tradingview tab is not TV."""
    with fake_cdp(non_tv_tabs()) as port:
        monkeypatch.setenv("CDP_HOST", "127.0.0.1")
        monkeypatch.setenv("TV_MCP_PORT_RANGE", f"{port}-{port}")
        with pytest.raises(cdp_discover.DiscoveryError):
            cdp_discover.discover_cdp()


def test_multiple_tv_prefers_cached(monkeypatch, _clean_env_and_cache):
    with fake_cdp(tv_tabs()) as p1, fake_cdp(tv_tabs()) as p2:
        # Use the HIGHER port in the cache to verify it's preferred over the
        # default "lowest match" ordering.
        target = max(p1, p2)
        _clean_env_and_cache.parent.mkdir(parents=True, exist_ok=True)
        _clean_env_and_cache.write_text(json.dumps(
            {"host": "127.0.0.1", "port": target,
             "discovered_at": "2026-01-01T00:00:00Z", "probe_url": ""}
        ))
        monkeypatch.setenv("CDP_HOST", "127.0.0.1")
        lo, hi = min(p1, p2), max(p1, p2)
        monkeypatch.setenv("TV_MCP_PORT_RANGE", f"{lo}-{hi}")
        # Force a rescan (force=True bypasses cache validate), but the
        # tiebreaker still consults the cache file to prefer the cached port.
        # Wait — force=True clears the cache lookup. We use the cache-validate
        # path instead: the cached port is alive AND in scan range, so the
        # cache-hit fast path returns it directly. Either way we land on `target`.
        host, p = cdp_discover.discover_cdp()
        assert (host, p) == ("127.0.0.1", target)


def test_no_tv_anywhere_raises(monkeypatch, _clean_env_and_cache):
    monkeypatch.setenv("CDP_HOST", "127.0.0.1")
    # Pick a range we're confident no process listens on locally.
    monkeypatch.setenv("TV_MCP_PORT_RANGE", "65510-65515")
    with pytest.raises(cdp_discover.DiscoveryError, match="No TradingView CDP endpoint"):
        cdp_discover.discover_cdp()


def test_force_refresh_skips_cache(monkeypatch, _clean_env_and_cache):
    with fake_cdp(tv_tabs()) as port:
        # Cache says dead-port; force=True must scan anyway.
        _clean_env_and_cache.parent.mkdir(parents=True, exist_ok=True)
        _clean_env_and_cache.write_text(json.dumps(
            {"host": "127.0.0.1", "port": 1,
             "discovered_at": "2026-01-01T00:00:00Z", "probe_url": ""}
        ))
        monkeypatch.setenv("CDP_HOST", "127.0.0.1")
        monkeypatch.setenv("TV_MCP_PORT_RANGE", f"{port}-{port}")
        host, p = cdp_discover.discover_cdp(force=True)
        assert (host, p) == ("127.0.0.1", port)


def test_invalid_port_range_uses_default(monkeypatch, _clean_env_and_cache, caplog):
    monkeypatch.setattr(cdp_discover, "_DEFAULT_PORT_RANGE", (59999, 59999))
    monkeypatch.setenv("CDP_HOST", "127.0.0.1")
    monkeypatch.setenv("TV_MCP_PORT_RANGE", "garbage")
    # No TV running on default range either, so we expect DiscoveryError after
    # falling back to the (9222-9230) default; the warning log is what we check.
    with pytest.raises(cdp_discover.DiscoveryError):
        cdp_discover.discover_cdp()
    assert any("invalid TV_MCP_PORT_RANGE" in r.message for r in caplog.records)


def test_atomic_cache_write(monkeypatch, _clean_env_and_cache):
    with fake_cdp(tv_tabs()) as port:
        monkeypatch.setenv("CDP_HOST", "127.0.0.1")
        monkeypatch.setenv("TV_MCP_PORT_RANGE", f"{port}-{port}")
        cdp_discover.discover_cdp()
        # No leftover tmp files in the cache dir
        leftover = [p for p in _clean_env_and_cache.parent.iterdir() if p.name.startswith(".tmp_")]
        assert leftover == []
