#!/usr/bin/env bash
# Verify the patched tradingview-mcp files are identical between:
#   - this repo:  data_svc/patches/{cdp_discover.js, connection.js, core_tab.js}
#   - trading repo (sibling): external/tradingview-mcp/src/{cdp_discover.js, connection.js, core/tab.js}
#
# Exits 1 if any file diverges. Intended for pre-commit / CI.
# When the trading repo's external/tradingview-mcp/ is eventually deleted
# (deferred cleanup), drop this check.

set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
TRADING="${TRADING_REPO:-${HOME}/projects/2026/trading}"

PAIRS=(
  "${HERE}/data_svc/patches/cdp_discover.js:${TRADING}/external/tradingview-mcp/src/cdp_discover.js"
  "${HERE}/data_svc/patches/connection.js:${TRADING}/external/tradingview-mcp/src/connection.js"
  "${HERE}/data_svc/patches/core_tab.js:${TRADING}/external/tradingview-mcp/src/core/tab.js"
)

fail=0
for pair in "${PAIRS[@]}"; do
  a="${pair%%:*}"
  b="${pair##*:}"
  if [[ ! -f "$a" ]] || [[ ! -f "$b" ]]; then
    echo "MISSING: $a or $b" >&2
    fail=1
    continue
  fi
  if ! diff -q "$a" "$b" >/dev/null; then
    echo "DIVERGED: $a vs $b" >&2
    diff "$a" "$b" >&2 || true
    fail=1
  fi
done

if [[ $fail -ne 0 ]]; then
  echo "Patch parity FAILED — patched files must be identical in both repos." >&2
  exit 1
fi
echo "Patch parity OK: $((${#PAIRS[@]})) files identical."
