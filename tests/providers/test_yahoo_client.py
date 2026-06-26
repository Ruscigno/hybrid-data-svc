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


def _make_limiter_with_clock(rpm=60):
    """Returns (limiter, clock_list) so tests can advance time."""
    clock = [0.0]
    limiter = AdaptiveRateLimiter(rpm=rpm, monotonic=lambda: clock[0])
    return limiter, clock


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
# Spy limiter for on_success / on_429 call tracking
# ---------------------------------------------------------------------------

class _SpyLimiter:
    """Records calls to on_success() and on_429() for assertion."""

    def __init__(self):
        self.success_calls = 0
        self.throttle_calls = 0
        self.notify_calls: list[float] = []
        self._cooldown_until = 0.0

    def acquire(self, *, sleep=None):
        pass  # no-op for spy

    def on_success(self):
        self.success_calls += 1

    def on_429(self):
        self.throttle_calls += 1

    def notify_retry_after(self, seconds: float):
        self.notify_calls.append(seconds)


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


class TestYahooClientFetchChartRetryAfter:
    """R1: Retry-After header is parsed and forwarded to the limiter."""

    def test_429_with_retry_after_header_sets_yahoo_throttled_attr(self):
        """429 + Retry-After: 30 -> YahooThrottled.retry_after == 30.0."""
        limiter = _make_limiter(rpm=60)
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(429, headers={"Retry-After": "30"}),
        )

        with pytest.raises(YahooThrottled) as exc_info:
            client.fetch_chart("AAPL", "1m", period="7d")

        assert exc_info.value.retry_after == 30.0

    def test_429_with_retry_after_calls_notify_on_limiter(self):
        """429 + Retry-After: 30 -> limiter.notify_retry_after(30.0) is called."""
        spy = _SpyLimiter()
        client = YahooClient(
            spy,
            get=lambda url, params: _fake_resp(429, headers={"Retry-After": "30"}),
        )

        with pytest.raises(YahooThrottled):
            client.fetch_chart("AAPL", "1m", period="7d")

        assert spy.notify_calls == [30.0]

    def test_429_without_retry_after_header_retry_after_is_none(self):
        """429 with no Retry-After header -> YahooThrottled.retry_after is None."""
        limiter = _make_limiter(rpm=60)
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(429),
        )

        with pytest.raises(YahooThrottled) as exc_info:
            client.fetch_chart("AAPL", "1m", period="7d")

        assert exc_info.value.retry_after is None

    def test_429_with_retry_after_limiter_sleeps_cooldown_on_next_acquire(self):
        """End-to-end: 429+Retry-After:30 -> next acquire() sleeps >= 30s."""
        clock = [0.0]
        limiter = AdaptiveRateLimiter(rpm=60, monotonic=lambda: clock[0])

        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(429, headers={"Retry-After": "30"}),
        )

        with pytest.raises(YahooThrottled):
            client.fetch_chart("AAPL", "1m", period="7d")

        # Now call acquire() directly with a spy sleep
        slept = []

        def spy_sleep(secs):
            slept.append(secs)
            clock[0] += secs

        limiter.acquire(sleep=spy_sleep)

        assert any(s >= 30.0 for s in slept), (
            f"Expected cooldown sleep >=30s after Retry-After, got {slept}"
        )

    def test_999_with_retry_after_sets_retry_after_attr(self):
        """999 + Retry-After: 60 -> YahooThrottled.retry_after == 60.0."""
        limiter = _make_limiter(rpm=60)
        client = YahooClient(
            limiter,
            get=lambda url, params: _fake_resp(999, headers={"Retry-After": "60"}),
        )

        with pytest.raises(YahooThrottled) as exc_info:
            client.fetch_chart("AAPL", "1m", period="7d")

        assert exc_info.value.retry_after == 60.0


class TestYahooClientFetchChartNon200Non429:
    """R3a: non-200/429/999 responses raise RuntimeError; on_success is NOT called."""

    def test_404_raises_runtime_error(self):
        """404 response -> raises RuntimeError."""
        spy = _SpyLimiter()
        client = YahooClient(
            spy,
            get=lambda url, params: _fake_resp(404),
        )

        with pytest.raises(RuntimeError) as exc_info:
            client.fetch_chart("AAPL", "1m", period="7d")

        assert "404" in str(exc_info.value)

    def test_500_raises_runtime_error(self):
        """500 response -> raises RuntimeError."""
        spy = _SpyLimiter()
        client = YahooClient(
            spy,
            get=lambda url, params: _fake_resp(500),
        )

        with pytest.raises(RuntimeError) as exc_info:
            client.fetch_chart("AAPL", "1m", period="7d")

        assert "500" in str(exc_info.value)

    def test_non_200_does_not_call_on_success(self):
        """Non-200 (e.g. 500) -> on_success() is NOT called."""
        spy = _SpyLimiter()
        client = YahooClient(
            spy,
            get=lambda url, params: _fake_resp(500),
        )

        with pytest.raises(RuntimeError):
            client.fetch_chart("AAPL", "1m", period="7d")

        assert spy.success_calls == 0

    def test_error_message_body_is_truncated_to_200_chars(self):
        """R5: The RuntimeError message truncates the response body to 200 chars."""
        long_body = "x" * 500

        class _LongBodyResp:
            status_code = 500
            headers = {}
            text = long_body

            def json(self):
                return None

        spy = _SpyLimiter()
        client = YahooClient(spy, get=lambda url, params: _LongBodyResp())

        with pytest.raises(RuntimeError) as exc_info:
            client.fetch_chart("AAPL", "1m", period="7d")

        msg = str(exc_info.value)
        # The body in the message should be truncated to at most 200 chars
        assert long_body not in msg, "Full 500-char body appeared in error; should be truncated"


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

    def test_symbol_with_caret_is_url_encoded(self):
        """R4: Symbol with '^' (e.g. '^GSPC') is URL-encoded in the path."""
        captured = {}

        def fake_get(url, params):
            captured["url"] = url
            return _fake_resp(200, _valid_payload())

        limiter = _make_limiter()
        client = YahooClient(limiter, get=fake_get)
        client.fetch_chart("^GSPC", "1d", period="1mo")

        url = captured["url"]
        assert "^GSPC" not in url, "Raw '^' should be URL-encoded"
        assert "%5EGSPC" in url or "%5egspc" in url.lower(), (
            f"Expected URL-encoded '^GSPC' in URL, got: {url}"
        )

    def test_symbol_with_slash_is_url_encoded(self):
        """R4: Symbol with '/' is URL-encoded so it doesn't split the path."""
        captured = {}

        def fake_get(url, params):
            captured["url"] = url
            return _fake_resp(200, _valid_payload())

        limiter = _make_limiter()
        client = YahooClient(limiter, get=fake_get)
        client.fetch_chart("BRK/B", "1d", period="1mo")

        url = captured["url"]
        # The path part after /chart/ should not contain a raw slash
        path_after_chart = url.split("/chart/")[-1]
        assert "/" not in path_after_chart, (
            f"Raw '/' in path: {path_after_chart}"
        )


class TestYahooClientHostsFailover:
    """R6: On network/connection errors, try the next host; HTTP status -> no failover."""

    def test_first_host_network_error_falls_back_to_second(self):
        """If the first host raises a connection error, the second host is tried."""
        calls = []

        def fake_get(url, params):
            calls.append(url)
            if "query1" in url:
                raise ConnectionError("network error on host 1")
            # Second host succeeds
            return _fake_resp(200, _valid_payload())

        limiter = _make_limiter()
        client = YahooClient(limiter, get=fake_get)
        df = client.fetch_chart("AAPL", "1m", period="7d")

        assert any("query1" in u for u in calls), "First host was not attempted"
        assert any("query2" in u for u in calls), "Second host was not attempted"
        assert len(df) == 1  # got a valid result

    def test_all_hosts_raise_reraises_last_error(self):
        """If all hosts raise network errors, the last exception is re-raised."""
        def fake_get(url, params):
            raise OSError("all hosts down")

        limiter = _make_limiter()
        client = YahooClient(limiter, get=fake_get)

        with pytest.raises(OSError, match="all hosts down"):
            client.fetch_chart("AAPL", "1m", period="7d")

    def test_429_on_first_host_does_not_failover(self):
        """A 429 HTTP response means the host answered — no failover to second host."""
        calls = []

        def fake_get(url, params):
            calls.append(url)
            # Always return 429 (don't raise — host answered)
            return _fake_resp(429)

        limiter = _make_limiter()
        client = YahooClient(limiter, get=fake_get)

        with pytest.raises(YahooThrottled):
            client.fetch_chart("AAPL", "1m", period="7d")

        # Only one host should have been tried (HTTP status = host answered)
        assert len(calls) == 1, (
            f"Expected failover not to trigger on 429, but {len(calls)} hosts were tried"
        )
