#!/usr/bin/env bash
#
# Install (or refresh) the TradingView Desktop watchdog LaunchAgent.
# Idempotent — bootout before bootstrap so running multiple times only
# re-installs the latest version.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_SRC="${SCRIPT_DIR}/com.tickerbeats.tv-desktop-watchdog.plist"
WATCHDOG_SH="${SCRIPT_DIR}/tv-watchdog.sh"
LABEL="com.tickerbeats.tv-desktop-watchdog"
PLIST_DST="${HOME}/Library/LaunchAgents/${LABEL}.plist"
UID_NUM="$(id -u)"

if [[ ! -f "${PLIST_SRC}" ]]; then
    echo "[install] error: ${PLIST_SRC} not found" >&2
    exit 1
fi
if [[ ! -f "${WATCHDOG_SH}" ]]; then
    echo "[install] error: ${WATCHDOG_SH} not found" >&2
    exit 1
fi

mkdir -p "${HOME}/Library/LaunchAgents"

# Render the plist with the placeholder resolved to the absolute path of
# this repo's tv-watchdog.sh. The committed plist stays operator-agnostic.
sed "s|@WATCHDOG_SH@|${WATCHDOG_SH}|g" "${PLIST_SRC}" > "${PLIST_DST}"
# tv-watchdog.sh ships with mode 755 in the repo (see git ls-files -s);
# no chmod needed here.

# Idempotent bootstrap: bootout first (silent if not loaded) so we always
# end up with the freshly-rendered plist active.
launchctl bootout "gui/${UID_NUM}/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/${UID_NUM}" "${PLIST_DST}"

# Fire once now so the operator sees the effect immediately instead of
# waiting up to 5 min.
launchctl kickstart "gui/${UID_NUM}/${LABEL}"

cat <<EOF
[install] LaunchAgent installed and triggered.
[install]   plist : ${PLIST_DST}
[install]   script: ${WATCHDOG_SH}
[install] verify:
[install]   launchctl list | grep ${LABEL}
[install]   log show --predicate 'subsystem == "tv-watchdog"' --last 5m
[install]   tail -f /tmp/tv-watchdog.log
EOF
