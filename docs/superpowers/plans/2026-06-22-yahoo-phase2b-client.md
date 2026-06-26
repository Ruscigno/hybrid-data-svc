# Yahoo Provider — Phase 2b: Client + Source abstraction — design brief

> Implements ADR 0002 **D9** (source abstraction) + **D12** (adaptive 429 auto-throttle), reusing the `experiments/yahoo_rate_probe` findings. Standalone new module off `main` — depends on nothing else (no Phase 1, no aggregation). Pure/injectable so it TDDs without network.

**Goal:** a `data_svc/providers/yahoo/` package that fetches 1-minute / daily OHLCV from Yahoo's chart endpoint via `curl_cffi` browser impersonation, behind a `Source` interface, with an in-process rpm rate limiter that auto-throttles on 429.

## Global constraints
- **`curl_cffi` is mandatory** (browser TLS impersonation). Yahoo's edge returns `429` to non-browser TLS stacks (`httpx`/`requests`/`curl`) on the *first* request; `curl_cffi` with `impersonate="chrome"` returns `200` + OHLCV. Add `curl_cffi>=0.7` to `requirements.txt`.
- **Testable without network:** the rate limiter takes injectable `monotonic`/`sleep`; the parser is pure; the client takes an injectable HTTP getter. Tests must NOT hit Yahoo.
- **Reference code (read, don't import):** `/Users/sander/projects/market-data-service/src/market_data_service/rate_limiter.py` (the 75%-on-429 / +5-rpm-per-30-success throttle — D12) and `.../sources/base.py` (the `OHLCVSource` Protocol + `get_source` factory — D9). Adapt, don't copy InfluxDB/Redis bits.
- Output DataFrame columns are exactly `["time","open","high","low","close","volume"]`, `time` = epoch seconds UTC, ascending. Matches what `data_svc/aggregate.py` consumes.
- Test env for the full suite (regression): `DOCKER_HOST=unix:///Users/sander/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true REST_AUTH_TOKEN= REST_ADMIN_TOKEN= /Users/sander/projects/hybrid-data-svc/.venv/bin/python -m pytest -q tests/`. The new module's own tests are pure (no Docker).

## Files
| File | Responsibility |
|---|---|
| `data_svc/providers/yahoo/__init__.py` | exports `YahooSource`, `YahooClient`, `get_source` |
| `data_svc/providers/yahoo/parse.py` | `parse_chart(payload) -> DataFrame` (pure) |
| `data_svc/providers/yahoo/ratelimit.py` | `RateLimiter` + `AdaptiveRateLimiter` (D12) |
| `data_svc/providers/yahoo/client.py` | `YahooClient` (curl_cffi + limiter + parse), `YahooThrottled` |
| `data_svc/providers/yahoo/source.py` | `Source` ABC, registry (`register_source`/`get_source`), `YahooSource` |
| `tests/providers/test_yahoo_parse.py` / `_ratelimit.py` / `_client.py` / `_source.py` | pure unit tests |
| `requirements.txt` | + `curl_cffi>=0.7` |

## Interfaces (exact signatures — downstream 2c writer relies on these)

### parse.py
```python
def parse_chart(payload: dict) -> pd.DataFrame:
    """Yahoo v8 chart JSON -> DataFrame[time,open,high,low,close,volume] (time=epoch s, UTC),
    sorted ascending. Drops rows where any of open/high/low/close is null. Returns an empty
    DataFrame with those columns for an empty/None/malformed payload (never raises on 'no data')."""
```
Payload shape: `payload["chart"]["result"][0]` has `timestamp: list[int]` and `indicators.quote[0]` with parallel `open/high/low/close/volume` lists (any entry may be `null`). `payload["chart"]["error"]` non-null ⇒ empty df.

### ratelimit.py
```python
class RateLimiter:
    def __init__(self, rpm: float, *, monotonic=time.monotonic) -> None: ...
    @property
    def rpm(self) -> float: ...
    def set_rpm(self, rpm: float) -> None: ...
    def acquire(self, *, sleep=time.sleep) -> None:
        """Even-pace requests to `rpm` per minute (interval = 60/rpm s). Blocks via `sleep`."""

class AdaptiveRateLimiter:           # D12: wraps a RateLimiter
    def __init__(self, rpm: float, *, floor_rpm=5.0, throttle_factor=0.75,
                 restore_step=5.0, restore_after=30, monotonic=time.monotonic) -> None: ...
    @property
    def rpm(self) -> float: ...
    def acquire(self, *, sleep=time.sleep) -> None: ...
    def on_429(self) -> None:   # rpm = max(floor_rpm, rpm*throttle_factor); reset success streak
    def on_success(self) -> None:  # streak += 1; every `restore_after` clean, rpm = min(base, rpm+restore_step), reset streak
```
`base` rpm = the initial rpm (never restore above it).

### client.py
```python
class YahooThrottled(Exception): ...

class YahooClient:
    HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
    def __init__(self, limiter: AdaptiveRateLimiter, *, impersonate: str = "chrome",
                 read_timeout: float = 15.0, get=None) -> None:
        # `get`: optional injectable callable(url, params) -> _Resp with .status_code, .headers, .json()/.text
        # default builds a curl_cffi.requests.Session(impersonate=...) and uses session.get.
    def fetch_chart(self, symbol: str, interval: str, *, period: str | None = None,
                    start: int | None = None, end: int | None = None) -> pd.DataFrame:
        """limiter.acquire(); GET https://{host}/v8/finance/chart/{symbol}
        with params {interval, range=period} OR {interval, period1=start, period2=end}.
        429/999 -> limiter.on_429() then raise YahooThrottled. 200 -> limiter.on_success();
        return parse_chart(resp.json()). Other status / network error -> raise (no on_success)."""
```

### source.py
```python
class Source(abc.ABC):
    name: str
    @abc.abstractmethod
    def fetch(self, symbol: str, interval: str, *, period=None, start=None, end=None) -> pd.DataFrame: ...

def register_source(name: str, factory) -> None: ...
def get_source(name: str, **kwargs) -> Source: ...   # raises KeyError/ValueError on unknown name

class YahooSource(Source):
    name = "yahoo"
    def __init__(self, client: YahooClient) -> None: ...
    def fetch(self, symbol, interval, *, period=None, start=None, end=None) -> pd.DataFrame:
        return self._client.fetch_chart(symbol, interval, period=period, start=start, end=end)
```
Register `"yahoo"` in the registry at import time (factory builds a `YahooClient` from a passed `rpm` or limiter).

## Required test cases (TDD — write first, watch fail, implement)

**parse**: (1) a small hand-built valid payload (3 timestamps, one row with a null close) → 2 rows, correct OHLCV, sorted; (2) `{"chart":{"result":None,"error":{"code":"Not Found"}}}` → empty df with columns; (3) `{}`/`None` → empty df.

**ratelimit**: (1) `RateLimiter(rpm=60)` with a fake clock+sleep: 3 `acquire()` calls request sleeps summing to ≥ ~2s of spacing (interval 1s) — assert on recorded sleep durations, deterministic via injected monotonic. (2) `AdaptiveRateLimiter(rpm=60).on_429()` → rpm == 45.0; repeated on_429 floors at `floor_rpm`. (3) `on_success` × `restore_after` after a throttle restores by `restore_step` but never above base.

**client** (inject a fake `get`): (1) fake returns 200 + valid payload → returns parsed df AND limiter.on_success was called; (2) fake returns status 429 → raises `YahooThrottled` AND limiter.on_429 called AND rpm dropped; (3) fake returns 200 + empty payload → empty df; (4) assert the URL/params built for `interval="1m", period="7d"` and for `start=.., end=..` (period1/period2). Use a fake limiter/spy or the real `AdaptiveRateLimiter` with injected clock.

**source**: (1) `get_source("yahoo", rpm=60)` returns a `YahooSource` whose `.fetch` delegates to the client (inject a fake client); (2) `get_source("nope")` raises.

## Commits
Logical commits (e.g. ratelimit / parse / client+source / requirements). Conventional messages. Run the new tests green, then the full suite green, before the final commit.
