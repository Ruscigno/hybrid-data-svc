# Yahoo Provider — Phase 2a: Aggregation Engine — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A pure, deterministic function that aggregates 1-minute OHLCV bars into each of the 12 target timeframes — the engine the Yahoo writer (Phase 2c) will use to derive all timeframes from fetched 1-minute data.

**Architecture:** One new standalone module `data_svc/aggregate.py` exposing `aggregate(df_1m, timeframe) -> pd.DataFrame`. Intraday timeframes bucket by fixed UTC second-windows; daily-and-up bucket by **US/Eastern** trading-session calendar. Pure function of its input (no clock, no DB, no network), so it is fully unit-testable. Independent of the Phase 1 provider work — touches no existing file except adding `tzdata` to `requirements.txt`.

**Tech Stack:** Python 3.12, pandas, `tzdata` (IANA tz for `America/New_York`), pytest.

## Global Constraints

- This is **Phase 2a of the Yahoo provider** ([ADR 0001](../../adr/0001-yahoo-finance-provider.md), decision D2 "pure 1-min aggregation"). It is a **standalone new module** — it does NOT depend on the Phase 1 PR (#16) being merged, and must not import from or modify Phase-1-touched files (`cache.py`, `service.py`, `feeds.py`, the migration). Build it on a branch off `main`.
- **The 12 timeframes and their storage codes** (verbatim — these strings are the `timeframe` argument and must be matched exactly):
  `1m→"1"`, `5m→"5"`, `15m→"15"`, `30m→"30"`, `1h→"1h"`, `2h→"2h"`, `4h→"4h"`, `8h→"8h"`, `1d→"1D"`, `3d→"3D"`, `1w→"1W"`, `1mo→"1M"`.
- **Input DataFrame shape:** columns exactly `["time", "open", "high", "low", "close", "volume"]`, where `time` is the bar-open **unix epoch seconds (UTC)**. Output has the same columns, `time` = the bucket-open epoch seconds, sorted ascending by `time`.
- **OHLCV aggregation rule (every timeframe):** within a bucket — `open` = value at the earliest `time`, `high` = max, `low` = min, `close` = value at the latest `time`, `volume` = sum.
- **Determinism:** `aggregate` takes NO `now`/clock argument. It buckets exactly the rows present, including the most-recent (possibly still-forming) bucket — the writer re-runs it each cycle and idempotently upserts, so the forming bar updates until it closes.
- **Eastern anchor:** daily/3-day/weekly/monthly buckets are keyed on the **`America/New_York`** local date (so a `"1D"` bar = one US trading session, not a UTC calendar day). Bucket-open `time` = that bucket's start date at **00:00 America/New_York**, as a UTC epoch.
- **Tests are pure** — no Docker/Postgres. Run a single file with `python -m pytest tests/test_aggregate.py -v`. Before the FINAL commit, run the full suite (which DOES need the testcontainer): `DOCKER_HOST=unix:///Users/sander/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true REST_AUTH_TOKEN= REST_ADMIN_TOKEN= /Users/sander/projects/hybrid-data-svc/.venv/bin/python -m pytest -q tests/`.

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `data_svc/aggregate.py` | `aggregate()` + intraday/calendar bucketing helpers | **Create** |
| `tests/test_aggregate.py` | pure unit tests for every timeframe | **Create** |
| `requirements.txt` | add `tzdata` (needed for `America/New_York` in slim containers) | Modify |

---

## Task 1: Module scaffold + 1-minute passthrough + intraday buckets

Intraday timeframes (`5,15,30,1h,2h,4h,8h`) bucket by fixed UTC second-windows; `1` is a normalized passthrough.

**Files:**
- Create: `data_svc/aggregate.py`
- Test: `tests/test_aggregate.py`

**Interfaces:**
- Produces: `aggregate(df_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame`. Module constants `INTRADAY_SECONDS: dict[str, int]` and `CALENDAR_TFS: frozenset[str]`. Raises `ValueError` on an unknown `timeframe`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_aggregate.py`:

```python
"""Pure unit tests for the 1-minute -> N-timeframe aggregation engine."""
from __future__ import annotations

import pandas as pd
import pytest

from data_svc.aggregate import aggregate

_COLS = ["time", "open", "high", "low", "close", "volume"]


def _df(rows: list[tuple]) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=_COLS)


def test_five_minute_bucket_ohlcv():
    # Five 1-min bars all inside the 5-min bucket starting at t0 (t0 % 300 == 0).
    t0 = 1_700_000_100
    df = _df([
        (t0 + 0,   10, 10, 10, 10, 1),
        (t0 + 60,  11, 15, 11, 11, 2),
        (t0 + 120, 12, 12,  9, 12, 3),
        (t0 + 180, 13, 13, 13, 13, 4),
        (t0 + 240, 14, 14, 14, 14, 5),
    ])
    out = aggregate(df, "5")
    assert list(out.columns) == _COLS
    assert len(out) == 1
    row = out.iloc[0]
    assert int(row["time"]) == t0
    assert row["open"] == 10 and row["close"] == 14
    assert row["high"] == 15 and row["low"] == 9
    assert row["volume"] == 15


def test_intraday_splits_into_two_buckets():
    t0 = 1_700_000_100  # 5-min aligned
    df = _df([
        (t0 + 0,   10, 10, 10, 10, 1),
        (t0 + 240, 14, 14, 14, 14, 1),
        (t0 + 300, 20, 25, 20, 22, 1),  # next 5-min bucket
    ])
    out = aggregate(df, "5")
    assert list(out["time"]) == [t0, t0 + 300]
    assert list(out["close"]) == [14, 22]


def test_one_minute_passthrough_sorts_and_dedups():
    df = _df([
        (200, 2, 2, 2, 2, 1),
        (100, 1, 1, 1, 1, 1),
        (200, 9, 9, 9, 9, 9),  # duplicate minute -> keep last
    ])
    out = aggregate(df, "1")
    assert list(out["time"]) == [100, 200]
    assert int(out.iloc[1]["close"]) == 9  # last write wins for the dup


def test_empty_input_returns_empty_with_columns():
    out = aggregate(_df([]), "1h")
    assert list(out.columns) == _COLS
    assert len(out) == 0


def test_unknown_timeframe_raises():
    with pytest.raises(ValueError):
        aggregate(_df([(0, 1, 1, 1, 1, 1)]), "7m")
```

- [ ] **Step 2: Run to verify failure**

Run: `/Users/sander/projects/hybrid-data-svc/.venv/bin/python -m pytest tests/test_aggregate.py -v`
Expected: FAIL — `data_svc.aggregate` does not exist (ImportError).

- [ ] **Step 3: Implement the module (intraday + passthrough)**

Create `data_svc/aggregate.py`:

```python
"""Aggregate 1-minute OHLCV bars into higher timeframes (ADR 0001, D2).

Pure function of its input: no clock, no DB, no network. Intraday timeframes
bucket by fixed UTC second-windows; daily-and-up bucket by the US/Eastern
trading-session calendar (added in a later task). The most-recent bucket may be
partial — the writer re-runs this each cycle and upserts idempotently.
"""
from __future__ import annotations

import pandas as pd

_COLS = ["time", "open", "high", "low", "close", "volume"]

# Storage code -> bucket width in seconds (UTC-anchored).
INTRADAY_SECONDS: dict[str, int] = {
    "5": 300, "15": 900, "30": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "8h": 28800,
}

# Daily-and-up timeframes, bucketed on the America/New_York calendar.
CALENDAR_TFS: frozenset[str] = frozenset({"1D", "3D", "1W", "1M"})

_AGG = {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Sort ascending by time and collapse duplicate minutes (last write wins)."""
    if df.empty:
        return pd.DataFrame(columns=_COLS)
    out = df[_COLS].sort_values("time", kind="stable")
    out = out.drop_duplicates(subset="time", keep="last").reset_index(drop=True)
    return out


def _bucket(df: pd.DataFrame, key: pd.Series) -> pd.DataFrame:
    """Group `df` by the integer bucket-open `key` and apply the OHLCV rule."""
    grouped = (
        df.assign(time=key.values)
        .groupby("time", sort=True, as_index=False)
        .agg(_AGG)
    )
    return grouped[_COLS].reset_index(drop=True)


def aggregate(df_1m: pd.DataFrame, timeframe: str) -> pd.DataFrame:
    """Aggregate 1-minute bars (columns _COLS, `time`=epoch seconds UTC) into
    `timeframe` (a storage code: "1","5",...,"8h","1D","3D","1W","1M")."""
    df = _normalize(df_1m)
    if timeframe == "1":
        return df
    if df.empty:
        return pd.DataFrame(columns=_COLS)
    if timeframe in INTRADAY_SECONDS:
        width = INTRADAY_SECONDS[timeframe]
        key = (df["time"].astype("int64") // width) * width
        return _bucket(df, key)
    if timeframe in CALENDAR_TFS:
        raise NotImplementedError("calendar timeframes land in Task 2/3")
    raise ValueError(f"unknown timeframe {timeframe!r}")
```

- [ ] **Step 4: Run to verify pass**

Run: `/Users/sander/projects/hybrid-data-svc/.venv/bin/python -m pytest tests/test_aggregate.py -v`
Expected: PASS — all five tests green (the `NotImplementedError` branch is not yet exercised).

- [ ] **Step 5: Commit**

```bash
git add data_svc/aggregate.py tests/test_aggregate.py
git commit -m "feat(aggregate): 1-min passthrough + intraday timeframe buckets"
```

---

## Task 2: US/Eastern calendar buckets — daily, weekly, monthly

`1D`/`1W`/`1M` bucket on the `America/New_York` local date so a daily bar is one trading session. DST-safe: derive the ET local date per row, map to the bucket's start date, then localize that date's midnight back to a UTC epoch.

**Files:**
- Modify: `data_svc/aggregate.py`
- Modify: `requirements.txt`
- Test: `tests/test_aggregate.py`

**Interfaces:**
- Consumes: `aggregate`, `_normalize`, `_bucket`, `CALENDAR_TFS` from Task 1.
- Produces: helper `_calendar_bucket_open(times: pd.Series, timeframe: str) -> pd.Series` returning the bucket-open epoch (int seconds) per row for `1D`/`1W`/`1M` (and `3D` in Task 3).

- [ ] **Step 1: Add the tz data dependency**

In `requirements.txt`, add a line (the engine needs the IANA `America/New_York` zone, which slim containers lack):

```
tzdata>=2024.1
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_aggregate.py`:

```python
# America/New_York is UTC-5 in January (EST). Reference epochs:
#   2024-01-03 00:00 ET = 1704258000   (ET midnight, that trading date)
#   2024-01-01 00:00 ET = 1704085200   (the ISO-week Monday, and Jan month start)
#   2024-02-01 00:00 ET = 1706763600   (Feb month start)
# Session bars on 2024-01-03 (09:30/15:00/15:59 ET = 14:30/20:00/20:59 UTC):
_D0930 = 1704292200
_D1500 = 1704312000
_D1559 = 1704315540


def test_daily_bar_is_one_eastern_session():
    df = _df([
        (_D0930, 100, 101,  99, 100, 5),
        (_D1500, 100, 110,  95, 105, 7),
        (_D1559, 105, 106, 104, 102, 3),
    ])
    out = aggregate(df, "1D")
    assert len(out) == 1
    row = out.iloc[0]
    assert int(row["time"]) == 1704258000  # 2024-01-03 00:00 ET
    assert row["open"] == 100 and row["close"] == 102
    assert row["high"] == 110 and row["low"] == 95
    assert row["volume"] == 15


def test_weekly_bar_anchors_to_eastern_monday():
    df = _df([(_D0930, 1, 1, 1, 1, 1), (_D1559, 2, 2, 2, 2, 1)])
    out = aggregate(df, "1W")
    assert len(out) == 1
    assert int(out.iloc[0]["time"]) == 1704085200  # Mon 2024-01-01 00:00 ET
    assert out.iloc[0]["close"] == 2


def test_monthly_bars_split_by_eastern_month():
    feb = 1706800000  # 2024-02-01 13:46 UTC, an ET-February instant
    df = _df([(_D0930, 1, 1, 1, 1, 1), (feb, 2, 2, 2, 2, 1)])
    out = aggregate(df, "1M")
    assert list(out["time"]) == [1704085200, 1706763600]  # Jan, Feb month-starts ET
```

- [ ] **Step 3: Run to verify failure**

Run: `/Users/sander/projects/hybrid-data-svc/.venv/bin/python -m pytest tests/test_aggregate.py -k calendar_or_daily -v` (or just the new test names)
Expected: FAIL — `aggregate(..., "1D")` raises `NotImplementedError`.

- [ ] **Step 4: Implement the calendar bucketing**

In `data_svc/aggregate.py`, add the import and helper, and route calendar timeframes through it. Add near the top:

```python
import datetime as _dt
```

Add this helper (above `aggregate`):

```python
_NY = "America/New_York"


def _et_midnight_epoch(d: _dt.date) -> int:
    """Epoch seconds of `d` at 00:00 America/New_York (DST-safe)."""
    return int(pd.Timestamp(d).tz_localize(_NY).timestamp())


def _calendar_bucket_open(times: pd.Series, timeframe: str) -> pd.Series:
    """Per-row bucket-open epoch (int seconds) for a calendar timeframe."""
    et_dates = (
        pd.to_datetime(times.astype("int64"), unit="s", utc=True)
        .dt.tz_convert(_NY)
        .dt.date
    )

    def start_date(d: _dt.date) -> _dt.date:
        if timeframe == "1D":
            return d
        if timeframe == "1W":
            return d - _dt.timedelta(days=d.weekday())      # back to Monday
        if timeframe == "1M":
            return d.replace(day=1)
        if timeframe == "3D":                               # fixed 3-day grid
            o = d.toordinal()
            return _dt.date.fromordinal(o - (o % 3))
        raise ValueError(f"unknown calendar timeframe {timeframe!r}")

    # Cache per distinct date so we localize each calendar date only once.
    opens = {d: _et_midnight_epoch(start_date(d)) for d in set(et_dates)}
    return et_dates.map(opens).astype("int64")
```

Replace the `if timeframe in CALENDAR_TFS:` branch in `aggregate` with:

```python
    if timeframe in CALENDAR_TFS:
        key = _calendar_bucket_open(df["time"], timeframe)
        return _bucket(df, key)
```

- [ ] **Step 5: Run to verify pass**

Run: `/Users/sander/projects/hybrid-data-svc/.venv/bin/python -m pytest tests/test_aggregate.py -v`
Expected: PASS — daily/weekly/monthly tests green; the Task 1 tests still green.

- [ ] **Step 6: Commit**

```bash
git add data_svc/aggregate.py tests/test_aggregate.py requirements.txt
git commit -m "feat(aggregate): Eastern-session daily/weekly/monthly buckets"
```

---

## Task 3: 3-day buckets (fixed Eastern grid)

`3D` is non-standard; define it as 3-consecutive-calendar-day buckets on a fixed grid anchored so that ET dates whose `toordinal() % 3 == 0` start a bucket. The `start_date` logic is already in `_calendar_bucket_open` from Task 2 — this task adds the test that locks the behavior.

**Files:**
- Test: `tests/test_aggregate.py`

**Interfaces:**
- Consumes: `aggregate` with `timeframe="3D"` (already wired via `CALENDAR_TFS` + `_calendar_bucket_open`).

- [ ] **Step 1: Write the failing test**

Append to `tests/test_aggregate.py`:

```python
def test_three_day_bucket_fixed_grid():
    # 2024-01-03 (ordinal 738888, %3==0) starts a 3-day bucket {01-03,01-04,01-05}.
    # 2024-01-06 (ordinal 738891, %3==0) starts the next bucket.
    d_0103 = _D0930              # 2024-01-03 14:30 UTC
    d_0104 = _D0930 + 86400      # 2024-01-04 14:30 UTC (same bucket)
    d_0106 = _D0930 + 3 * 86400  # 2024-01-06 14:30 UTC (next bucket)
    df = _df([
        (d_0103, 10, 10, 10, 10, 1),
        (d_0104, 20, 30, 20, 25, 1),
        (d_0106, 40, 40, 40, 40, 1),
    ])
    out = aggregate(df, "3D")
    assert list(out["time"]) == [1704258000, 1704517200]  # 01-03 ET, 01-06 ET
    first = out.iloc[0]
    assert first["open"] == 10 and first["close"] == 25 and first["high"] == 30
    assert int(out.iloc[1]["open"]) == 40
```

- [ ] **Step 2: Run to verify it passes (behavior already implemented)**

Run: `/Users/sander/projects/hybrid-data-svc/.venv/bin/python -m pytest tests/test_aggregate.py::test_three_day_bucket_fixed_grid -v`
Expected: PASS — `_calendar_bucket_open`'s `"3D"` branch (Task 2) already implements the fixed-grid logic; this test confirms it. (If it fails, fix `start_date`'s `"3D"` branch, not the test.)

- [ ] **Step 3: Run the full aggregation file + the whole suite**

Run: `/Users/sander/projects/hybrid-data-svc/.venv/bin/python -m pytest tests/test_aggregate.py -v`
Expected: PASS — all aggregation tests.

Then the full suite (regression; needs the testcontainer):
Run: `DOCKER_HOST=unix:///Users/sander/.colima/default/docker.sock TESTCONTAINERS_RYUK_DISABLED=true REST_AUTH_TOKEN= REST_ADMIN_TOKEN= /Users/sander/projects/hybrid-data-svc/.venv/bin/python -m pytest -q tests/`
Expected: PASS — existing suite unaffected (the engine is a new module; only `requirements.txt` changed besides new files).

- [ ] **Step 4: Commit**

```bash
git add tests/test_aggregate.py
git commit -m "test(aggregate): lock 3-day fixed-grid Eastern bucketing"
```

---

## Self-Review

**Spec coverage (vs ADR D2 + the 12 timeframes):**
- `1` passthrough → Task 1. ✅
- Intraday `5,15,30,1h,2h,4h,8h` (UTC buckets) → Task 1. ✅
- `1D,1W,1M` (Eastern session) → Task 2. ✅
- `3D` (fixed Eastern grid) → Tasks 2+3. ✅
- OHLCV rule (first/max/min/last/sum) → `_AGG`, used everywhere. ✅
- Deterministic, no clock; forming bucket included → `aggregate` has no `now`. ✅
- `tzdata` dependency for `America/New_York` → Task 2. ✅

**Placeholder scan:** none — every step has complete code + exact commands/expected results.

**Type consistency:** `aggregate(df_1m, timeframe) -> pd.DataFrame`; helpers `_normalize`, `_bucket`, `_calendar_bucket_open`, `_et_midnight_epoch` are defined before use; `INTRADAY_SECONDS`/`CALENDAR_TFS`/`_AGG`/`_COLS` referenced consistently; storage codes match the Global Constraints table exactly.

**Out of scope (later Phase 2 sub-plans):** the Yahoo client/parser (2b), the `feeds` migration + writer that calls `aggregate` (2c), config + onboarding + REST `?provider=` (2d). This plan delivers only the engine.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-06-19-yahoo-phase2a-aggregation-engine.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — fresh subagent per task, review between tasks.

**2. Inline Execution** — execute here with checkpoints.

**Which approach?**
