"""Tests for data_svc/providers/yahoo/client.py — pure, no network, injectable get."""

import pytest
import pandas as pd

from data_svc.providers.yahoo.client import YahooClient, YahooThrottled
from data_svc.providers.yahoo.ratelimit import AdaptiveRateLimiter


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------

def _make_limiter(rpm=60):
    """AdaptiveRateLimiter with a frozen clock so acquire() never sleeps."""
    clock = [0.0]
    return AdaptiveRateLimiter(rpm=rpm, monotonic=lambda: clock[0])


def _fake_resp(status_code, payload=None, headers=None):
    """Minimal fake HTTP response object."""
    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.headers = headers or {}
            self._payload = payload

        def json(self):
            return self._payload

        @property
        def text(self):
            return str(self._payload)

    return _Resp()


def _valid_payload(symbol="AAPL"):
    """Minimal valid Yahoo chart payload with one row of data."""
    return {
        "chart": {
            "result": [
                {
                    "timestamp": [1_700_000_000],
                    "indicators": {
                        "quote": [
                            {
                                "open": [150.0],
                                "high": [155.0],
                                "low": [149.0],
                                "close": [154.0],
                                "volume": [1_000_000],
                            }
                        ]
                    },
                }
            ],
            "error": None,
        }
    }


def _empty_payload():
    return {"chart": {"result": None, "error": None}}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestYahooClientFetchChart200:
    def test_200_valid_payload_returns_dataframe(self):
        """200 + valid payload -> non-empty DataFrame with OHLCV columns."""
        limiter = _make_limiter()
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(200, _valid_payload()),
        )

        df = client.fetch_chart("AAPL", "1m", period="7d")

        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["time", "open", "high", "low", "close", "volume"]
        assert len(df) == 1

    def test_200_calls_on_success_on_limiter(self):
        """200 response -> limiter.on_success() is called (rpm stays at base)."""
        limiter = _make_limiter(rpm=60)
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(200, _valid_payload()),
        )

        client.fetch_chart("AAPL", "1m", period="7d")

        # on_success increments the streak; rpm should NOT have changed (no prior throttle)
        assert limiter.rpm == 60.0

    def test_200_empty_payload_returns_empty_dataframe(self):
        """200 + empty payload -> empty DataFrame with correct columns."""
        limiter = _make_limiter()
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(200, _empty_payload()),
        )

        df = client.fetch_chart("AAPL", "1m", period="7d")

        assert isinstance(df, pd.DataFrame)
        assert list(df.columns) == ["time", "open", "high", "low", "close", "volume"]
        assert len(df) == 0


class TestYahooClientFetchChart429:
    def test_429_raises_yahoo_throttled(self):
        """429 response -> raises YahooThrottled."""
        limiter = _make_limiter(rpm=60)
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(429),
        )

        with pytest.raises(YahooThrottled):
            client.fetch_chart("AAPL", "1m", period="7d")

    def test_429_calls_on_429_on_limiter(self):
        """429 response -> limiter.on_429() is called -> rpm drops."""
        limiter = _make_limiter(rpm=60)
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(429),
        )

        with pytest.raises(YahooThrottled):
            client.fetch_chart("AAPL", "1m", period="7d")

        assert limiter.rpm == 45.0  # 60 * 0.75

    def test_999_raises_yahoo_throttled(self):
        """999 response (Yahoo soft-block) -> raises YahooThrottled."""
        limiter = _make_limiter(rpm=60)
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(999),
        )

        with pytest.raises(YahooThrottled):
            client.fetch_chart("AAPL", "1m", period="7d")

    def test_999_calls_on_429_on_limiter(self):
        """999 response -> limiter.on_429() is called -> rpm drops."""
        limiter = _make_limiter(rpm=60)
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(999),
        )

        with pytest.raises(YahooThrottled):
            client.fetch_chart("AAPL", "1m", period="7d")

        assert limiter.rpm == 45.0


class TestYahooClientUrlParams:
    def test_period_param_builds_range_query(self):
        """interval + period -> params include {interval, range=period}."""
        captured = {}

        def fake_get(url, params):
            captured["url"] = url
            captured["params"] = params
            return _fake_resp(200, _valid_payload())

        limiter = _make_limiter()
        client = YahooClient(limiter, get=fake_get)
        client.fetch_chart("AAPL", "1m", period="7d")

        assert "interval" in captured["params"]
        assert captured["params"]["interval"] == "1m"
        assert "range" in captured["params"]
        assert captured["params"]["range"] == "7d"
        assert "period1" not in captured["params"]
        assert "period2" not in captured["params"]

    def test_start_end_params_build_period1_period2(self):
        """interval + start + end -> params include {interval, period1=start, period2=end}."""
        captured = {}

        def fake_get(url, params):
            captured["url"] = url
            captured["params"] = params
            return _fake_resp(200, _valid_payload())

        limiter = _make_limiter()
        client = YahooClient(limiter, get=fake_get)
        client.fetch_chart("AAPL", "1m", start=1_700_000_000, end=1_700_086_400)

        assert captured["params"]["interval"] == "1m"
        assert captured["params"]["period1"] == 1_700_000_000
        assert captured["params"]["period2"] == 1_700_086_400
        assert "range" not in captured["params"]

    def test_url_contains_symbol_and_yahoo_host(self):
        """URL uses one of the Yahoo Finance hosts and contains the symbol."""
        captured = {}

        def fake_get(url, params):
            captured["url"] = url
            return _fake_resp(200, _valid_payload())

        limiter = _make_limiter()
        client = YahooClient(limiter, get=fake_get)
        client.fetch_chart("TSLA", "1d", period="1mo")

        url = captured["url"]
        assert "TSLA" in url
        assert any(host in url for host in YahooClient.HOSTS)
        assert "/v8/finance/chart/" in url
