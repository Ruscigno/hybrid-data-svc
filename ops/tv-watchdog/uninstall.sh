#!/usr/bin/env bash
#
# Remove the TradingView Desktop watchdog LaunchAgent.

set -euo pipefail

LABEL="com.tickerbeats.tv-desktop-watchdog"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"

launchctl bootout "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
rm -f "${PLIST_DST}"

echo "[uninstall] removed ${LABEL}."
echo "[uninstall] note: /tmp/tv-watchdog.{log,launchd.out,launchd.err} left intact for review."
