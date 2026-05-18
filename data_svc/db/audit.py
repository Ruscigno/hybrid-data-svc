"""
Periodic integrity audit of the bars table.

Read-only scan that detects two classes of corruption:
  1. Gaps  — missing bars in the expected ts sequence per (symbol, timeframe)
  2. Off-grid bars — ts that do not align to the timeframe's natural grid
     (the same grid the insert-time guard in cache.py enforces)

The audit does NOT auto-fix. Corruption is rare enough that a human should
review before any DELETE/INSERT. The audit's job is observability — log a
warning so the operator sees it on the next /analyze-bots or log review.
"""
from __future__ import annotations

import logging
from typing import NamedTuple

from .postgres import get_pool

logger = logging.getLogger(__name__)


# Inlined as SQL VALUES so the audit runs in a single round-trip.
# Mirror of cache._TF_SECONDS — keep in sync.
_TF_SECS_VALUES = (
    "('1',60),('3',180),('5',300),('15',900),('30',1800),"
    "('60',3600),('1h',3600),('2h',7200),('4h',14400),"
    "('D',86400),('1D',86400),('W',604800),('1W',604800)"
)


class AuditReport(NamedTuple):
    total_combos: int
    gap_free: int
    with_gaps: int
    with_extras: int
    off_grid_bars: int
    total_bars: int
    problems: list[dict]  # [{"combo": "BTC/1h", "kind": "gaps", "count": 3}, ...]


def audit_integrity(postgres_url: str) -> AuditReport:
    """Read-only scan of the bars table. Returns an AuditReport."""
    pool = get_pool(postgres_url)

    sql_combos = f"""
        WITH tf_secs(tf, secs) AS (VALUES {_TF_SECS_VALUES}),
             combos AS (
                 SELECT b.symbol, b.timeframe, t.secs::int AS secs,
                        MIN(b.ts) AS min_ts, MAX(b.ts) AS max_ts,
                        COUNT(*) AS actual
                 FROM bars b JOIN tf_secs t ON t.tf=b.timeframe
                 GROUP BY 1,2,3
             )
        SELECT symbol, timeframe, secs, min_ts, max_ts, actual,
               ((max_ts - min_ts) / secs + 1) AS expected
        FROM combos
    """

    sql_off_grid = f"""
        WITH tf_secs(tf, secs) AS (VALUES {_TF_SECS_VALUES}),
             mins AS (
                 SELECT b.symbol, b.timeframe, t.secs::int AS secs,
                        MIN(b.ts) AS min_ts
                 FROM bars b JOIN tf_secs t ON t.tf=b.timeframe
                 GROUP BY 1,2,3
             )
        SELECT b.symbol, b.timeframe, COUNT(*) AS off_grid_count
        FROM bars b
        JOIN mins m USING (symbol, timeframe)
        WHERE ((b.ts - m.min_ts) % m.secs) <> 0
        GROUP BY 1,2
    """

    with pool.connection() as conn:
        combos = conn.execute(sql_combos).fetchall()
        off_grid_rows = conn.execute(sql_off_grid).fetchall()

    gap_free = sum(1 for c in combos if c[6] == c[5])
    with_gaps = sum(1 for c in combos if c[6] > c[5])
    with_extras = sum(1 for c in combos if c[6] < c[5])
    off_grid_bars = sum(int(r[2]) for r in off_grid_rows)
    total_bars = sum(int(c[5]) for c in combos)

    problems: list[dict] = []
    for c in combos:
        sym, tf, _secs, _mn, _mx, actual, expected = c
        if expected > actual:
            problems.append({"combo": f"{sym}/{tf}", "kind": "gaps", "count": int(expected - actual)})
        elif expected < actual:
            problems.append({"combo": f"{sym}/{tf}", "kind": "extras", "count": int(actual - expected)})
    for r in off_grid_rows:
        sym, tf, n = r
        problems.append({"combo": f"{sym}/{tf}", "kind": "off_grid", "count": int(n)})

    return AuditReport(
        total_combos=len(combos),
        gap_free=gap_free,
        with_gaps=with_gaps,
        with_extras=with_extras,
        off_grid_bars=off_grid_bars,
        total_bars=total_bars,
        problems=problems,
    )


def format_audit(report: AuditReport) -> str:
    """One-line summary plus per-problem detail lines, joined with '\\n'."""
    if not report.problems:
        return (
            f"[AUDIT] healthy — {report.gap_free}/{report.total_combos} combos gap-free, "
            f"0 off-grid bars, {report.total_bars} total bars"
        )
    head = (
        f"[AUDIT] issues found — {report.gap_free}/{report.total_combos} gap-free, "
        f"with_gaps={report.with_gaps}, with_extras={report.with_extras}, "
        f"off_grid_bars={report.off_grid_bars}"
    )
    lines = [head]
    for p in report.problems[:15]:
        lines.append(f"  - {p['combo']}: {p['kind']}={p['count']}")
    if len(report.problems) > 15:
        lines.append(f"  ... + {len(report.problems) - 15} more")
    return "\n".join(lines)
