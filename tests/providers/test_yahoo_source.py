"""Tests for data_svc/providers/yahoo/source.py — pure, no network."""

import pytest
import pandas as pd

from data_svc.providers.yahoo.source import (
    Source,
    get_source,
    register_source,
    YahooSource,
)
from data_svc.providers.yahoo.client import YahooClient
from data_svc.providers.yahoo.ratelimit import AdaptiveRateLimiter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_limiter(rpm=60):
    clock = [0.0]
    return AdaptiveRateLimiter(rpm=rpm, monotonic=lambda: clock[0])


def _fake_resp(status_code, payload=None):
    class _Resp:
        def __init__(self):
            self.status_code = status_code
            self.headers = {}
            self._payload = payload

        def json(self):
            return self._payload

        @property
        def text(self):
            return str(self._payload)

    return _Resp()


def _valid_payload():
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


# ---------------------------------------------------------------------------
# Fake client for delegation tests
# ---------------------------------------------------------------------------

class _FakeClient:
    """Records calls to fetch_chart for assertion."""

    def __init__(self, return_value=None):
        self.calls = []
        if return_value is None:
            return_value = pd.DataFrame(
                columns=["time", "open", "high", "low", "close", "volume"]
            )
        self._return_value = return_value

    def fetch_chart(self, symbol, interval, *, period=None, start=None, end=None):
        self.calls.append(
            {"symbol": symbol, "interval": interval, "period": period,
             "start": start, "end": end}
        )
        return self._return_value


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestYahooSource:
    def test_yahoo_source_name_is_yahoo(self):
        """YahooSource.name == 'yahoo'."""
        client = _FakeClient()
        src = YahooSource(client)
        assert src.name == "yahoo"

    def test_fetch_delegates_to_client_fetch_chart(self):
        """YahooSource.fetch() delegates to client.fetch_chart with same args."""
        client = _FakeClient()
        src = YahooSource(client)

        src.fetch("AAPL", "1m", period="7d")

        assert len(client.calls) == 1
        call = client.calls[0]
        assert call["symbol"] == "AAPL"
        assert call["interval"] == "1m"
        assert call["period"] == "7d"
        assert call["start"] is None
        assert call["end"] is None

    def test_fetch_passes_start_end_to_client(self):
        """YahooSource.fetch() passes start/end kwargs through to client."""
        client = _FakeClient()
        src = YahooSource(client)

        src.fetch("TSLA", "1d", start=1_700_000_000, end=1_700_086_400)

        call = client.calls[0]
        assert call["start"] == 1_700_000_000
        assert call["end"] == 1_700_086_400
        assert call["period"] is None

    def test_fetch_returns_dataframe_from_client(self):
        """YahooSource.fetch() returns the DataFrame from client."""
        expected = pd.DataFrame(
            {"time": [1_700_000_000], "open": [150.0], "high": [155.0],
             "low": [149.0], "close": [154.0], "volume": [1_000_000]}
        )
        client = _FakeClient(return_value=expected)
        src = YahooSource(client)

        result = src.fetch("AAPL", "1m", period="7d")

        assert result is expected

    def test_yahoo_source_is_subclass_of_source_abc(self):
        """YahooSource is a subclass of Source ABC."""
        assert issubclass(YahooSource, Source)


class TestGetSource:
    def test_get_source_yahoo_returns_yahoo_source(self):
        """get_source('yahoo', rpm=60) returns a YahooSource instance."""
        src = get_source("yahoo", rpm=60)
        assert isinstance(src, YahooSource)
        assert src.name == "yahoo"

    def test_get_source_unknown_raises_key_or_value_error(self):
        """get_source('nope') raises KeyError or ValueError."""
        with pytest.raises((KeyError, ValueError)):
            get_source("nope")

    def test_get_source_yahoo_fetch_delegates_to_client(self):
        """get_source('yahoo') returns a source that uses a real YahooClient."""
        # We need to inject a fake getter so it doesn't hit the network.
        def fake_get(url, params):
            return _fake_resp(200, _valid_payload())

        src = get_source("yahoo", rpm=60, get=fake_get)
        df = src.fetch("AAPL", "1m", period="1d")

        assert isinstance(df, pd.DataFrame)
        assert len(df) == 1

    def test_register_and_get_custom_source(self):
        """register_source + get_source round-trip for a custom name."""

        class _DummySource(Source):
            name = "dummy"

            def __init__(self, **kwargs):
                pass

            def fetch(self, symbol, interval, *, period=None, start=None, end=None):
                return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

        register_source("dummy", lambda **kw: _DummySource(**kw))

        src = get_source("dummy")
        assert isinstance(src, _DummySource)
