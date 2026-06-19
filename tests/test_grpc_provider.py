from __future__ import annotations

from data_svc.db.cache import BarCache
from data_svc.grpc_server.proto import bars_pb2 as _pb
from data_svc.grpc_server.service import BarServiceServicer


def _servicer(pg_url):
    return BarServiceServicer(BarCache(pg_url), pg_url)


def test_get_recent_bars_precedence_and_override(pg_url, reset_db, seed_bar):
    seed_bar("AAA", "1h", 3600, close=1.0, provider="tradingview")
    seed_bar("AAA", "1h", 3600, close=2.0, provider="yahoo")
    svc = _servicer(pg_url)

    # No provider -> TradingView precedence.
    resp = svc.GetRecentBars(
        _pb.GetRecentBarsRequest(symbol="AAA", timeframe="1h", count=10), None)
    assert resp.bars[-1].close == 1.0

    # Explicit yahoo override.
    resp = svc.GetRecentBars(
        _pb.GetRecentBarsRequest(symbol="AAA", timeframe="1h", count=10, provider="yahoo"), None)
    assert resp.bars[-1].close == 2.0


def test_get_recent_bars_falls_back_to_yahoo(pg_url, reset_db, seed_bar):
    seed_bar("BBB", "1h", 3600, close=7.0, provider="yahoo")  # only yahoo
    svc = _servicer(pg_url)
    resp = svc.GetRecentBars(
        _pb.GetRecentBarsRequest(symbol="BBB", timeframe="1h", count=10), None)
    assert resp.bars[-1].close == 7.0
