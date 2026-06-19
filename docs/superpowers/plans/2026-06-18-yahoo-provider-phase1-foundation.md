# Yahoo Provider — Phase 1: Multi-provider Foundation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `provider` dimension to bar storage and gRPC serving so bars can come from multiple sources, with per-series TradingView precedence and an explicit provider override — provable entirely against existing TradingView data, before any Yahoo code exists.

**Architecture:** Add a `provider` column (default `'tradingview'`) to `bars` and `cache_meta`, extend their primary keys, and thread an optional `provider` argument through the `BarCache` read/write methods (defaulting to `'tradingview'` so every existing caller is unchanged). A new `BarCache.resolve_provider()` implements per-series precedence (TradingView wins, else Yahoo). The gRPC `BarService` gains an optional `provider` request field and calls the resolver. REST and the `feeds` table are intentionally **out of scope** for this phase.

**Tech Stack:** Python 3.12, Postgres 16 (psycopg3 + `psycopg_pool`), gRPC (`buf generate`), pytest + `testcontainers[postgres]`, pandas.

## Global Constraints

- This is **Phase 1 of 5** from [ADR 0001](../../adr/0001-yahoo-finance-provider.md). Phases 2–5 (Yahoo client/writer, aggregation, backfill, catalog) get their own plans.
- **Deviation from ADR migration `004`:** the ADR bundled `feeds` into migration 004. This plan splits it — Phase 1's migration touches **only `bars` + `cache_meta`**; the `feeds` provider column + `tv_symbol`→`provider_symbol` rename move to Phase 2 (where the Yahoo writer needs them). Rationale: the `feeds` rename ripples into `feeds.py`, `assets_service.py`, `feeds_loader.py`, and catalog tests with zero benefit to the serving foundation.
- **Backward compatibility is mandatory:** every new `provider` parameter defaults to `'tradingview'`; existing rows backfill to `'tradingview'`. The TradingView writer path and the entire existing test suite must stay green at every commit.
- **Provider precedence order** is the tuple `("tradingview", "yahoo")` — first provider with data for `(symbol, timeframe)` wins when the caller doesn't specify one.
- **Run tests** with `make test` (runs `pytest -q tests/`). On a local colima Docker, prefix with `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true` (testcontainers requirement). Tests skip gracefully if Docker is unavailable.
- **Branch:** execute on a fresh feature branch off `main` (e.g. `feat/yahoo-provider-foundation`), NOT the throwaway `yahoo-rate-probe` probe branch. The ADR/plan docs will be moved onto that branch at execution start.

---

## File structure

| File | Responsibility | Change |
|---|---|---|
| `migrations/004_provider.sql` | provider column + PK + index on `bars`/`cache_meta`; backfill | **Create** |
| `data_svc/db/cache.py` | provider-aware reads/writes + `resolve_provider` | Modify |
| `data_svc/grpc_server/proto/bars.proto` | optional `provider` on bar requests | Modify |
| `data_svc/grpc_server/proto/bars_pb2.py` etc. | regenerated stubs | Modify (via `make proto`) |
| `data_svc/grpc_server/service.py` | resolve + pass provider through | Modify |
| `tests/conftest.py` | `seed_bar` gains `provider`, fixes ON CONFLICT, writes `cache_meta` | Modify |
| `tests/test_cache_provider.py` | cache-level provider read/write + resolver tests | **Create** |
| `tests/test_grpc_provider.py` | gRPC-level precedence/override tests | **Create** |

---

## Task 1: Migration + provider-aware writes

Adds the `provider` column and makes inserts write it, keeping the suite green via defaults. The `feeds` table is untouched.

**Files:**
- Create: `migrations/004_provider.sql`
- Modify: `data_svc/db/cache.py` (`_insert_bars`, `_last_close`)
- Modify: `tests/conftest.py` (`seed_bar` fixture)
- Test: `tests/test_cache_provider.py`

**Interfaces:**
- Produces: `BarCache._insert_bars(self, df, symbol, timeframe, provider="tradingview")`; `BarCache._last_close(self, symbol, timeframe, provider="tradingview")`. `bars`/`cache_meta` now have a `provider TEXT NOT NULL DEFAULT 'tradingview'` column in their primary keys. The `seed_bar` fixture gains a `provider="tradingview"` kwarg and writes a matching `cache_meta` row.

- [ ] **Step 1: Write the migration**

Create `migrations/004_provider.sql`:

```sql
-- Phase 1 of the Yahoo provider (ADR 0001): add a provider dimension to bar
-- storage so multiple sources coexist. feeds is handled in a later migration.
-- Idempotent (IF EXISTS/IF NOT EXISTS) so re-running is safe.

-- bars ----------------------------------------------------------------------
ALTER TABLE bars ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'tradingview';
ALTER TABLE bars DROP CONSTRAINT IF EXISTS bars_pkey;
ALTER TABLE bars ADD PRIMARY KEY (symbol, timeframe, provider, ts);
DROP INDEX IF EXISTS idx_bars_symbol_tf_ts;
CREATE INDEX IF NOT EXISTS idx_bars_symbol_tf_provider_ts
    ON bars (symbol, timeframe, provider, ts DESC);

-- cache_meta ----------------------------------------------------------------
ALTER TABLE cache_meta ADD COLUMN IF NOT EXISTS provider TEXT NOT NULL DEFAULT 'tradingview';
ALTER TABLE cache_meta DROP CONSTRAINT IF EXISTS cache_meta_pkey;
ALTER TABLE cache_meta ADD PRIMARY KEY (symbol, timeframe, provider);
```

- [ ] **Step 2: Update the `seed_bar` fixture** (it breaks on the new PK otherwise)

In `tests/conftest.py`, replace the `seed_bar` fixture body with (adds `provider`, fixes the `ON CONFLICT` target, and upserts `cache_meta` so the resolver has inventory to read):

```python
@pytest.fixture
def seed_bar(pg_url: str):
    """Insert a single bar (+ its cache_meta row) directly into Postgres."""
    import psycopg

    def _seed(
        storage_symbol: str,
        timeframe: str,
        ts: int,
        close: float = 100.0,
        open_: float | None = None,
        high: float | None = None,
        low: float | None = None,
        volume: float = 1.0,
        provider: str = "tradingview",
    ) -> None:
        if open_ is None:
            open_ = close
        if high is None:
            high = close
        if low is None:
            low = close
        with psycopg.connect(pg_url, autocommit=True) as conn:
            conn.execute(
                """INSERT INTO bars
                     (symbol, timeframe, provider, ts, open, high, low, close, volume, fetched_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, EXTRACT(epoch FROM now())::bigint)
                   ON CONFLICT (symbol, timeframe, provider, ts) DO NOTHING""",
                (storage_symbol, timeframe, provider, ts, open_, high, low, close, volume),
            )
            conn.execute(
                """INSERT INTO cache_meta
                     (symbol, timeframe, provider, last_bar_ts, bar_count, last_fetched_at)
                   VALUES (%s, %s, %s, %s,
                           (SELECT COUNT(*) FROM bars
                              WHERE symbol=%s AND timeframe=%s AND provider=%s),
                           EXTRACT(epoch FROM now())::bigint)
                   ON CONFLICT (symbol, timeframe, provider) DO UPDATE SET
                     last_bar_ts=GREATEST(cache_meta.last_bar_ts, EXCLUDED.last_bar_ts),
                     bar_count=EXCLUDED.bar_count,
                     last_fetched_at=EXCLUDED.last_fetched_at""",
                (storage_symbol, timeframe, provider, ts, storage_symbol, timeframe, provider),
            )

    return _seed
```

- [ ] **Step 3: Write the failing test**

Create `tests/test_cache_provider.py`:

```python
from __future__ import annotations

from data_svc.db.cache import BarCache


def test_seed_and_count_are_provider_scoped(pg_url, reset_db, seed_bar):
    cache = BarCache(pg_url)
    seed_bar("AAA", "1h", 3600, close=1.0, provider="tradingview")
    seed_bar("AAA", "1h", 3600, close=2.0, provider="yahoo")

    # Two providers hold a bar at the same (symbol, timeframe, ts); both persist.
    assert cache.bar_count("AAA", "1h", "tradingview") == 1
    assert cache.bar_count("AAA", "1h", "yahoo") == 1
```

- [ ] **Step 4: Run it to verify it fails**

Run: `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true pytest tests/test_cache_provider.py::test_seed_and_count_are_provider_scoped -v`
Expected: FAIL — `bar_count()` does not yet accept a `provider` argument (`TypeError`).

- [ ] **Step 5: Thread `provider` through the write + `_last_close`**

In `data_svc/db/cache.py`, change `_insert_bars`'s signature and its two SQL statements. Signature:

```python
    def _insert_bars(self, df: pd.DataFrame, symbol: str, timeframe: str,
                     provider: str = "tradingview") -> None:
```

The `rows` comprehension (was lines 367-377) becomes:

```python
        rows = [
            (
                symbol, timeframe, provider,
                int(row["time"]),
                float(row["open"]), float(row["high"]),
                float(row["low"]),  float(row["close"]),
                float(row["volume"]),
                now,
            )
            for _, row in closed_df.iterrows()
        ]
```

The bars INSERT (was lines 382-391):

```python
                    cur.executemany(
                        """INSERT INTO bars
                             (symbol, timeframe, provider, ts, open, high, low, close, volume, fetched_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                           ON CONFLICT (symbol, timeframe, provider, ts) DO UPDATE SET
                             open=EXCLUDED.open, high=EXCLUDED.high, low=EXCLUDED.low,
                             close=EXCLUDED.close, volume=EXCLUDED.volume,
                             fetched_at=EXCLUDED.fetched_at""",
                        rows,
                    )
```

The cache_meta upsert (was lines 393-407):

```python
                    cur.execute(
                        """INSERT INTO cache_meta
                             (symbol, timeframe, provider, last_bar_ts, bar_count, last_fetched_at)
                           VALUES (
                             %s, %s, %s,
                             %s,
                             (SELECT COUNT(*) FROM bars
                                WHERE symbol=%s AND timeframe=%s AND provider=%s),
                             %s
                           )
                           ON CONFLICT (symbol, timeframe, provider) DO UPDATE SET
                             last_bar_ts=EXCLUDED.last_bar_ts,
                             bar_count=EXCLUDED.bar_count,
                             last_fetched_at=EXCLUDED.last_fetched_at""",
                        (symbol, timeframe, provider, last_ts, symbol, timeframe, provider, now),
                    )
```

Then make the layer-2 drift guard provider-scoped by changing its `_last_close` call (was line 338) to `self._last_close(symbol, timeframe, provider)`, and update `_last_close` (was lines 310-317):

```python
    def _last_close(self, symbol: str, timeframe: str,
                    provider: str = "tradingview") -> Optional[float]:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT close FROM bars WHERE symbol=%s AND timeframe=%s AND provider=%s "
                "ORDER BY ts DESC LIMIT 1",
                (symbol, timeframe, provider),
            ).fetchone()
        return float(row[0]) if row else None
```

- [ ] **Step 6: Add `provider` to `bar_count` (minimal, to make the test pass)**

In `data_svc/db/cache.py`, update `bar_count` and the `_get_meta` it delegates to. Change `bar_count` (was lines 162-164):

```python
    def bar_count(self, symbol: str, timeframe: str,
                  provider: str = "tradingview") -> int:
        meta = self._get_meta(symbol, timeframe, provider)
        return int(meta["bar_count"]) if meta else 0
```

And `_get_meta` (was lines 245-258):

```python
    def _get_meta(self, symbol: str, timeframe: str,
                  provider: str = "tradingview") -> Optional[dict]:
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT last_bar_ts, bar_count, last_fetched_at "
                "FROM cache_meta WHERE symbol=%s AND timeframe=%s AND provider=%s",
                (symbol, timeframe, provider),
            ).fetchone()
        if row is None:
            return None
        return {
            "last_bar_ts": int(row[0]),
            "bar_count": int(row[1]),
            "last_fetched_at": int(row[2]),
        }
```

- [ ] **Step 7: Run the new test + the full suite**

Run: `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true make test`
Expected: PASS — the new test passes AND every pre-existing test still passes (existing callers use the `'tradingview'` defaults; `seed_bar`'s new `cache_meta` write is additive).

- [ ] **Step 8: Commit**

```bash
git add migrations/004_provider.sql data_svc/db/cache.py tests/conftest.py tests/test_cache_provider.py
git commit -m "feat(db): provider column + provider-aware writes (bars, cache_meta)"
```

---

## Task 2: Provider-aware reads + precedence resolver

Make every read path filter by provider, and add `resolve_provider()` implementing per-series TradingView precedence.

**Files:**
- Modify: `data_svc/db/cache.py` (`read_bars`, `_read_bars`, `latest_bar`, `get_bars_in_range`, `latest_bar_ts`, `_invalidate`, `_validate_overlap`; add `resolve_provider`)
- Test: `tests/test_cache_provider.py`

**Interfaces:**
- Consumes: the provider-aware schema + writes from Task 1.
- Produces:
  - `BarCache.read_bars(self, symbol, timeframe, count, provider="tradingview") -> pd.DataFrame`
  - `BarCache.latest_bar(self, symbol, timeframe, provider="tradingview") -> Optional[dict]`
  - `BarCache.get_bars_in_range(self, symbol, timeframe, from_ts, to_ts, limit, provider="tradingview") -> tuple[list[dict], bool]`
  - `BarCache.latest_bar_ts(self, symbol, timeframe, provider="tradingview") -> Optional[int]`
  - `BarCache.resolve_provider(self, symbol, timeframe, requested="") -> str`
  - Module constant `PROVIDER_PRECEDENCE = ("tradingview", "yahoo")`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cache_provider.py`:

```python
def test_reads_filter_by_provider(pg_url, reset_db, seed_bar):
    cache = BarCache(pg_url)
    seed_bar("AAA", "1h", 3600, close=1.0, provider="tradingview")
    seed_bar("AAA", "1h", 3600, close=2.0, provider="yahoo")

    assert cache.read_bars("AAA", "1h", 10, "tradingview").iloc[-1]["close"] == 1.0
    assert cache.read_bars("AAA", "1h", 10, "yahoo").iloc[-1]["close"] == 2.0
    assert cache.latest_bar("AAA", "1h", "yahoo")["close"] == 2.0
    rows, _ = cache.get_bars_in_range("AAA", "1h", 0, 10_000, 10, "yahoo")
    assert rows[-1]["close"] == 2.0


def test_resolve_provider_precedence(pg_url, reset_db, seed_bar):
    cache = BarCache(pg_url)
    # Both present -> TradingView wins.
    seed_bar("AAA", "1h", 3600, provider="tradingview")
    seed_bar("AAA", "1h", 3600, provider="yahoo")
    assert cache.resolve_provider("AAA", "1h", "") == "tradingview"
    # Only Yahoo present -> falls back to Yahoo.
    seed_bar("BBB", "1h", 3600, provider="yahoo")
    assert cache.resolve_provider("BBB", "1h", "") == "yahoo"
    # Explicit request always wins, even past precedence.
    assert cache.resolve_provider("AAA", "1h", "yahoo") == "yahoo"
    # Nothing present -> default to the first precedence entry.
    assert cache.resolve_provider("ZZZ", "1h", "") == "tradingview"
```

- [ ] **Step 2: Run to verify failure**

Run: `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true pytest tests/test_cache_provider.py -v`
Expected: FAIL — `read_bars`/`latest_bar`/`get_bars_in_range` don't accept `provider`; `resolve_provider` doesn't exist.

- [ ] **Step 3: Add the precedence constant + resolver**

Near the top of `data_svc/db/cache.py` (with the other module constants), add:

```python
PROVIDER_PRECEDENCE = ("tradingview", "yahoo")
```

Add this method to `BarCache`:

```python
    def resolve_provider(self, symbol: str, timeframe: str,
                         requested: str = "") -> str:
        """Pick the provider to serve for (symbol, timeframe).

        Explicit `requested` always wins. Otherwise return the first provider
        in PROVIDER_PRECEDENCE that has inventory in cache_meta; if none do,
        fall back to the first precedence entry (an empty read)."""
        if requested:
            return requested
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT provider FROM cache_meta WHERE symbol=%s AND timeframe=%s",
                (symbol, timeframe),
            ).fetchall()
        present = {r[0] for r in rows}
        for p in PROVIDER_PRECEDENCE:
            if p in present:
                return p
        return PROVIDER_PRECEDENCE[0]
```

- [ ] **Step 4: Thread `provider` through the read methods**

In `data_svc/db/cache.py`:

`read_bars` (was lines 155-156):

```python
    def read_bars(self, symbol: str, timeframe: str, count: int,
                  provider: str = "tradingview") -> pd.DataFrame:
        return self._read_bars(symbol, timeframe, count, provider)
```

`_read_bars` (was lines 409-424) — add `provider` param + `AND provider=%s`:

```python
    def _read_bars(self, symbol: str, timeframe: str, count: int,
                   provider: str = "tradingview") -> pd.DataFrame:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ts, open, high, low, close, volume
                         FROM bars
                        WHERE symbol=%s AND timeframe=%s AND provider=%s
                        ORDER BY ts DESC
                        LIMIT %s""",
                    (symbol, timeframe, provider, int(count)),
                )
                rows = cur.fetchall()
        if not rows:
            return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])
        df = pd.DataFrame(rows, columns=["time", "open", "high", "low", "close", "volume"])
        return df.iloc[::-1].reset_index(drop=True)
```

`latest_bar` (was lines 166-190) — add `provider` param + `AND provider=%s`:

```python
    def latest_bar(self, symbol: str, timeframe: str,
                   provider: str = "tradingview") -> Optional[dict]:
        with self._pool.connection() as conn:
            row = conn.execute(
                """SELECT ts, open, high, low, close, volume
                     FROM bars
                    WHERE symbol=%s AND timeframe=%s AND provider=%s
                    ORDER BY ts DESC
                    LIMIT 1""",
                (symbol, timeframe, provider),
            ).fetchone()
        if row is None:
            return None
        return {
            "ts": int(row[0]), "open": float(row[1]), "high": float(row[2]),
            "low": float(row[3]), "close": float(row[4]), "volume": float(row[5]),
        }
```

`get_bars_in_range` (was lines 192-239) — add `provider` param + `AND provider=%s` in the WHERE; keep the rest identical:

```python
    def get_bars_in_range(self, symbol: str, timeframe: str, from_ts: int,
                          to_ts: int, limit: int,
                          provider: str = "tradingview") -> tuple[list[dict], bool]:
        if to_ts < from_ts:
            return ([], False)
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT ts, open, high, low, close, volume
                         FROM bars
                        WHERE symbol=%s AND timeframe=%s AND provider=%s
                          AND ts BETWEEN %s AND %s
                        ORDER BY ts DESC
                        LIMIT %s""",
                    (symbol, timeframe, provider, int(from_ts), int(to_ts), int(limit) + 1),
                )
                rows = cur.fetchall()
        truncated = len(rows) > limit
        kept = rows[:limit]
        kept.reverse()
        return (
            [
                {"ts": int(r[0]), "open": float(r[1]), "high": float(r[2]),
                 "low": float(r[3]), "close": float(r[4]), "volume": float(r[5])}
                for r in kept
            ],
            truncated,
        )
```

`latest_bar_ts` (was lines 158-160) — delegate with provider:

```python
    def latest_bar_ts(self, symbol: str, timeframe: str,
                      provider: str = "tradingview") -> Optional[int]:
        meta = self._get_meta(symbol, timeframe, provider)
        return meta["last_bar_ts"] if meta else None
```

- [ ] **Step 5: Make the TV-path internals provider-consistent**

`_invalidate` (was lines 297-308) deletes the cache for a (symbol, timeframe); scope it to a provider so a future Yahoo invalidation can't wipe TradingView rows. Add `provider: str = "tradingview"` and `AND provider=%s` to both its DELETEs. `get_bars` and `_validate_overlap` are the TradingView fetch path — leave their call sites using the default (they implicitly pass `'tradingview'`). No behavior change for TradingView.

- [ ] **Step 6: Run tests**

Run: `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true make test`
Expected: PASS — new provider tests pass; the full existing suite still passes.

- [ ] **Step 7: Commit**

```bash
git add data_svc/db/cache.py tests/test_cache_provider.py
git commit -m "feat(db): provider-aware reads + per-series precedence resolver"
```

---

## Task 3: gRPC `provider` field + serving precedence

Expose `provider` on the bar requests and have the servicer resolve + pass it. Default (unset) behavior is unchanged for existing clients (resolver returns `'tradingview'` when that's all that exists).

**Files:**
- Modify: `data_svc/grpc_server/proto/bars.proto`
- Modify (generated): `data_svc/grpc_server/proto/bars_pb2.py`, `bars_pb2.pyi`, `bars_pb2_grpc.py` (via `make proto`)
- Modify: `data_svc/grpc_server/service.py` (`GetRecentBars`, `GetBarsInRange`, `HealthCheck`)
- Test: `tests/test_grpc_provider.py`

**Interfaces:**
- Consumes: `BarCache.resolve_provider`, `read_bars`, `get_bars_in_range`, `bar_count`, `latest_bar_ts` (all provider-aware, Task 2).
- Produces: `GetRecentBarsRequest.provider`, `GetBarsInRangeRequest.provider`, `HealthRequest.provider` proto string fields; the servicer resolves them before reading.

- [ ] **Step 1: Add the proto fields**

In `data_svc/grpc_server/proto/bars.proto`, add a `provider` field to three messages (use the next free field number in each):

```proto
message GetRecentBarsRequest {
  string symbol = 1;
  string timeframe = 2;
  int32 count = 3;
  string provider = 4; // optional; empty => per-series precedence (TV wins)
}

message GetBarsInRangeRequest {
  string symbol = 1;
  string timeframe = 2;
  int64 from_ts = 3;
  int64 to_ts = 4;
  int32 limit = 5;
  string provider = 6; // optional; empty => per-series precedence
}

message HealthRequest {
  string symbol = 1;
  string timeframe = 2;
  int32 min_bars = 3;
  string provider = 4; // optional; empty => per-series precedence
}
```

- [ ] **Step 2: Regenerate the stubs**

Run: `make proto`
Expected: `data_svc/grpc_server/proto/bars_pb2.py`, `bars_pb2.pyi`, `bars_pb2_grpc.py` are rewritten with the new `provider` fields and no other diff.

- [ ] **Step 3: Write the failing test**

Create `tests/test_grpc_provider.py`:

```python
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
```

- [ ] **Step 4: Run to verify failure**

Run: `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true pytest tests/test_grpc_provider.py -v`
Expected: FAIL — the servicer ignores `provider` and always reads `'tradingview'`, so `test_get_recent_bars_falls_back_to_yahoo` returns no bars and the override assertion fails.

- [ ] **Step 5: Resolve + pass provider in the servicer**

In `data_svc/grpc_server/service.py`, update the three handlers to resolve the provider and pass it. `GetRecentBars` (was lines 30-47) — add two lines and pass `provider`:

```python
    def GetRecentBars(self, request, context):  # noqa: N802 (gRPC naming)
        symbol = request.symbol
        timeframe = request.timeframe
        count = max(1, min(int(request.count or 300), _MAX_BARS))
        provider = self._cache.resolve_provider(symbol, timeframe, request.provider)

        df = self._cache.read_bars(symbol, timeframe, count, provider)
        bars = [
            _pb.Bar(
                ts=int(row["time"]), open=float(row["open"]), high=float(row["high"]),
                low=float(row["low"]), close=float(row["close"]), volume=float(row["volume"]),
            )
            for _, row in df.iterrows()
        ]
        return _pb.BarsResponse(bars=bars, truncated=False)
```

`GetBarsInRange` (was lines 49-69) — resolve + pass provider:

```python
    def GetBarsInRange(self, request, context):  # noqa: N802
        symbol = request.symbol
        timeframe = request.timeframe
        from_ts = int(request.from_ts)
        to_ts = int(request.to_ts)
        requested = int(request.limit) if request.limit else _DEFAULT_RANGE_LIMIT
        limit = max(1, min(requested, _MAX_BARS))
        provider = self._cache.resolve_provider(symbol, timeframe, request.provider)

        rows, truncated = self._cache.get_bars_in_range(
            symbol, timeframe, from_ts, to_ts, limit, provider)
        bars = [
            _pb.Bar(
                ts=int(r["ts"]), open=float(r["open"]), high=float(r["high"]),
                low=float(r["low"]), close=float(r["close"]), volume=float(r["volume"]),
            )
            for r in rows
        ]
        return _pb.BarsResponse(bars=bars, truncated=truncated)
```

`HealthCheck` (was lines 71-83) — resolve + pass provider:

```python
    def HealthCheck(self, request, context):  # noqa: N802
        symbol = request.symbol
        timeframe = request.timeframe
        min_bars = int(request.min_bars or 200)
        provider = self._cache.resolve_provider(symbol, timeframe, request.provider)

        count = self._cache.bar_count(symbol, timeframe, provider)
        last_ts = self._cache.latest_bar_ts(symbol, timeframe, provider) or 0
        ready = count >= min_bars
        return _pb.HealthResponse(
            ready=ready, bars_available=int(count), last_bar_ts=int(last_ts),
        )
```

- [ ] **Step 6: Run the new test + full suite**

Run: `DOCKER_HOST=unix://$HOME/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true make test`
Expected: PASS — gRPC precedence/override/fallback tests pass; existing suite (including REST tests that go through the in-process gRPC stub) still passes, because unspecified-provider reads resolve to `'tradingview'` exactly as before.

- [ ] **Step 7: Commit**

```bash
git add data_svc/grpc_server/proto/ data_svc/grpc_server/service.py tests/test_grpc_provider.py
git commit -m "feat(grpc): optional provider field + per-series TV precedence in BarService"
```

---

## Self-Review

**Spec coverage (vs ADR D1 + D3, Phase 1 scope):**
- D1 provider column on `bars`/`cache_meta` → Task 1 migration. ✅ (`feeds` deferred to Phase 2 — documented deviation.)
- D3 per-series TV precedence + explicit override → `resolve_provider` (Task 2) + servicer wiring (Task 3). ✅
- gRPC `provider` request field → Task 3. ✅
- REST `?provider=` → **deferred to Phase 2** (no Yahoo data exists in Phase 1; documented). ✅
- Backfill existing TV rows → column `DEFAULT 'tradingview'` (Task 1). ✅

**Placeholder scan:** No TBD/TODO; every code step shows complete code; every run step has an exact command + expected result. ✅

**Type consistency:** `provider` is a `str` defaulting to `"tradingview"` in every method; `PROVIDER_PRECEDENCE` is referenced consistently; `resolve_provider(symbol, timeframe, requested="")` signature matches its call sites in Task 3. The `seed_bar` fixture's new `provider` kwarg matches every test call. ✅

**Out-of-scope, intentionally:** `feeds` table, `feeds.py`, `assets_service.py`, REST routers/openapi, the Yahoo client/writer/aggregation/catalog. These are Phases 2–5.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-18-yahoo-provider-phase1-foundation.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?** (And first: create the feature branch off `main` per the Global Constraints, and move ADR 0001 + this plan onto it.)
