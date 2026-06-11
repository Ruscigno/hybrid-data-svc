#!/usr/bin/env bash
#
# TradingView Desktop watchdog — keeps TV.app exposing CDP on a port within
# $TV_CDP_PORT_RANGE with at least one TradingView tab open, so the
# hybrid-data-svc writer can discover the endpoint without crash-looping.
#
# See docs/superpowers/specs/2026-06-10-tv-desktop-watchdog-design.md for
# the design rationale and state machine.

set -euo pipefail

# ---------------------------------------------------------------------------
# Tunables (env overrides; defaults are production-sane).
# ---------------------------------------------------------------------------
: "${TV_CDP_PORT:=9222}"           # Port used on relaunch (canonical default)
: "${TV_CDP_PORT_RANGE:=9222-9230}" # Range scanned during PROBE — mirrors
                                    # data_svc/cdp_discover.py default so the
                                    # watchdog doesn't thrash TV when it's
                                    # healthy on 9223+.
: "${TV_PROBE_TIMEOUT_S:=2}"
: "${TV_QUIT_TIMEOUT_S:=10}"
: "${TV_KILL_TIMEOUT_S:=5}"
: "${TV_VERIFY_TIMEOUT_S:=60}"
: "${TV_APP_PATH:=/Applications/TradingView.app}"
: "${DRY_RUN:=0}"
: "${LOCK_DIR:=/tmp/tv-watchdog.lock.d}"
: "${LOG_FILE:=/tmp/tv-watchdog.log}"

# Two thresholds matter for the stale-lock recovery:
#   * the PID inside the lock is no longer alive  -> always reclaim
#   * the lock is older than _LOCK_MAX_AGE_S      -> reclaim defensively
# The second guard is for cases where the PID was reused by an unrelated
# process before we could check (rare but theoretically possible).
: "${_LOCK_MAX_AGE_S:=180}"  # 3 min — comfortably > worst-case 80s run

readonly TV_BIN="${TV_APP_PATH}/Contents/MacOS/TradingView"
# Same regex pattern used by data_svc/cdp_discover.py:_is_tv_endpoint:
# a generic "tradingview" hit (anywhere in the URL) is enough — the chart
# subpath is the strong signal but not strictly required.
readonly TV_GENERIC_RE='tradingview'

# Generate a short run id so log lines across one execution can be correlated.
RUN_ID="$(openssl rand -hex 4 2>/dev/null || printf '%08x' "$$")"
readonly RUN_ID

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
log() {
    local level="$1"; shift
    local msg="$*"
    local ts
    ts="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"
    local line="${ts} [run=${RUN_ID}] ${level} ${msg}"
    # Two destinations only — verified empirically:
    #   1) /tmp/tv-watchdog.log (file, append, ISO 8601). Canonical.
    #   2) stdout/stderr → launchd captures these into
    #      /tmp/tv-watchdog.launchd.{out,err}. Secondary.
    # macOS `logger` does NOT reliably bridge to Unified Logging or syslog
    # on recent builds (ASL bridge is off by default), so a syslog
    # destination would be invisible noise. The file log is the only
    # query-friendly path; LOG_FILE has a sane default and is env-overridable.
    printf '%s\n' "${line}" >> "${LOG_FILE}" 2>/dev/null || true
    if [[ "${level}" == "ERROR" || "${level}" == "WARN" ]]; then
        printf '%s\n' "${line}" >&2
    else
        printf '%s\n' "${line}"
    fi
}

# ---------------------------------------------------------------------------
# Lock — mkdir is atomic on POSIX, no extra deps needed (flock isn't on macOS).
# Includes stale-lock recovery: a leftover lock dir (process died without the
# EXIT trap firing — SIGKILL, kernel panic, power loss) is reclaimed when
# either the PID inside is dead OR the directory is older than _LOCK_MAX_AGE_S.
# ---------------------------------------------------------------------------
_lock_pid_file() { printf '%s' "${LOCK_DIR}/owner.pid"; }

_lock_age_s() {
    if [[ ! -d "${LOCK_DIR}" ]]; then
        printf '0'
        return
    fi
    # macOS stat: -f %m gives mtime in epoch seconds.
    local mtime now
    mtime="$(stat -f '%m' "${LOCK_DIR}" 2>/dev/null || echo 0)"
    now="$(date -u +%s)"
    printf '%d' "$(( now - mtime ))"
}

_lock_owner_alive() {
    # Returns 0 if the PID recorded inside LOCK_DIR is still alive on this
    # host, 1 otherwise (including: no PID file, parse failure, dead PID).
    local pid_file pid
    pid_file="$(_lock_pid_file)"
    [[ -f "${pid_file}" ]] || return 1
    pid="$(cat "${pid_file}" 2>/dev/null || true)"
    [[ -n "${pid}" && "${pid}" =~ ^[0-9]+$ ]] || return 1
    # kill -0 doesn't send a signal, just probes liveness + permission.
    kill -0 "${pid}" 2>/dev/null
}

# Threshold below which a "missing owner.pid" doesn't trigger reclaim —
# closes the acquire_lock race (mkdir succeeds, then pid file is written).
# A legitimate writer has milliseconds; anything past this is genuinely
# orphan.
: "${_LOCK_PID_GRACE_S:=2}"

_reclaim_lock_if_stale() {
    # Returns 0 (and removes the lock) when the existing lock is provably
    # orphaned; 1 (lock left alone) when a live owner is still inside or
    # might still be acquiring.
    local age has_pid_file
    age="$(_lock_age_s)"
    [[ -f "$(_lock_pid_file)" ]] && has_pid_file=1 || has_pid_file=0

    if (( has_pid_file == 0 )); then
        # owner.pid absent. Could be (a) legitimate acquirer between mkdir
        # and the pid write, or (b) a crashed process that mkdir'd and died
        # before writing. Distinguish by age — a real acquirer is at most
        # a few ms away from writing the pid.
        if (( age < _LOCK_PID_GRACE_S )); then
            return 1
        fi
        log WARN "lock at ${LOCK_DIR} has no owner.pid after ${age}s; reclaiming"
        rm -rf "${LOCK_DIR}" 2>/dev/null
        return 0
    fi
    if ! _lock_owner_alive; then
        log WARN "lock at ${LOCK_DIR} has dead owner (age=${age}s); reclaiming"
        rm -rf "${LOCK_DIR}" 2>/dev/null
        return 0
    fi
    if (( age > _LOCK_MAX_AGE_S )); then
        log WARN "lock at ${LOCK_DIR} is alive but too old (age=${age}s > ${_LOCK_MAX_AGE_S}s); reclaiming"
        rm -rf "${LOCK_DIR}" 2>/dev/null
        return 0
    fi
    return 1
}

acquire_lock() {
    if mkdir "${LOCK_DIR}" 2>/dev/null; then
        printf '%s\n' "$$" > "$(_lock_pid_file)" 2>/dev/null || true
        # shellcheck disable=SC2064
        trap "rm -rf '${LOCK_DIR}' 2>/dev/null || true" EXIT
        return 0
    fi
    # Lock taken — first check if it's actually stale.
    if _reclaim_lock_if_stale; then
        if mkdir "${LOCK_DIR}" 2>/dev/null; then
            printf '%s\n' "$$" > "$(_lock_pid_file)" 2>/dev/null || true
            # shellcheck disable=SC2064
            trap "rm -rf '${LOCK_DIR}' 2>/dev/null || true" EXIT
            return 0
        fi
    fi
    return 1
}

# ---------------------------------------------------------------------------
# CDP probe + validation
# ---------------------------------------------------------------------------
_parse_port_range() {
    # Echo "lo hi" — defaults to "9222 9230" on parse failure.
    local spec="${1:-}"
    if [[ "${spec}" =~ ^([0-9]+)-([0-9]+)$ ]]; then
        printf '%s %s' "${BASH_REMATCH[1]}" "${BASH_REMATCH[2]}"
    else
        printf '9222 9230'
    fi
}

cdp_version_reachable_on() {
    # Returns 0 when /json/version returns 2xx on the given port.
    local port="$1"
    local code
    code="$(curl -sS -o /dev/null -m "${TV_PROBE_TIMEOUT_S}" \
        -w '%{http_code}' "http://localhost:${port}/json/version" 2>/dev/null || echo 000)"
    [[ "${code}" =~ ^2[0-9][0-9]$ ]]
}

cdp_has_tv_tab_on() {
    # Returns 0 when /json/list on the given port mentions tradingview
    # anywhere; 1 otherwise. Mirrors _is_tv_endpoint in cdp_discover.py.
    local port="$1"
    local body
    body="$(curl -sS -m "${TV_PROBE_TIMEOUT_S}" \
        "http://localhost:${port}/json/list" 2>/dev/null || true)"
    [[ -n "${body}" ]] || return 1
    printf '%s' "${body}" | grep -qiE "${TV_GENERIC_RE}"
}

healthy_port() {
    # Scans TV_CDP_PORT_RANGE; echoes the first port whose CDP is up AND has
    # at least one tradingview tab. Empty echo + non-zero exit when none.
    local range lo hi p
    range="$(_parse_port_range "${TV_CDP_PORT_RANGE}")"
    lo="${range%% *}"
    hi="${range##* }"
    for (( p = lo; p <= hi; p++ )); do
        if cdp_version_reachable_on "${p}" && cdp_has_tv_tab_on "${p}"; then
            printf '%s' "${p}"
            return 0
        fi
    done
    return 1
}

any_cdp_with_no_tab() {
    # Detects the "CDP up but no chart tab" warning case. Echoes the port
    # number when a port has /json/version OK but no tradingview tab in
    # /json/list. Returns 1 when no such port exists in the range.
    local range lo hi p
    range="$(_parse_port_range "${TV_CDP_PORT_RANGE}")"
    lo="${range%% *}"
    hi="${range##* }"
    for (( p = lo; p <= hi; p++ )); do
        if cdp_version_reachable_on "${p}" && ! cdp_has_tv_tab_on "${p}"; then
            printf '%s' "${p}"
            return 0
        fi
    done
    return 1
}

# ---------------------------------------------------------------------------
# Process detection / control
# ---------------------------------------------------------------------------
tv_main_pid() {
    # Print the PID of the TV main process, or empty when not running.
    # Filters out Electron Helper children so we don't try to kill those
    # directly — quitting the main process tears them down.
    ps -ax -o pid=,command= 2>/dev/null \
        | awk -v bin="${TV_BIN}" '
            index($0, bin) > 0 && index($0, "Helper") == 0 {
                print $1
                exit
            }
        '
}

tv_running() {
    [[ -n "$(tv_main_pid)" ]]
}

graceful_quit() {
    # Asks TV.app to quit via AppleScript. Stderr of osascript is captured
    # so a denied Automation permission produces an actionable log line
    # instead of a guess. Returns whatever exit code osascript reported.
    if [[ "${DRY_RUN}" == "1" ]]; then
        log INFO "DRY_RUN would: osascript quit TradingView"
        return 0
    fi
    local err_out rc=0
    err_out="$(osascript -e 'tell application "TradingView" to quit' 2>&1 >/dev/null)" || rc=$?
    if (( rc != 0 )); then
        # Make the actual osascript error visible (the most common one is
        # "Not authorized to send Apple events to TradingView." which means
        # Automation permission needs to be granted in System Settings).
        local trimmed="${err_out//$'\n'/ }"
        log WARN "osascript quit failed (rc=${rc}): ${trimmed:-<no stderr>}"
    fi
    return "${rc}"
}

wait_for_exit() {
    # Poll tv_running every 1s up to $1 seconds. Returns 0 when TV has
    # gone away, 1 on timeout.
    local timeout="$1"
    local waited=0
    while (( waited < timeout )); do
        if ! tv_running; then
            return 0
        fi
        sleep 1
        waited=$(( waited + 1 ))
    done
    ! tv_running
}

force_kill() {
    # SIGTERM the main TV process (best-effort), then escalate to SIGKILL
    # if TERM didn't clean up in time. `env kill` forces external/PATH
    # lookup over the Bash builtin so tests can shim it.
    local pid
    pid="$(tv_main_pid)"
    [[ -n "${pid}" ]] || return 0
    if [[ "${DRY_RUN}" == "1" ]]; then
        log INFO "DRY_RUN would: kill -TERM ${pid}; possibly -KILL"
        return 0
    fi
    env kill -TERM "${pid}" 2>/dev/null || true
    wait_for_exit "${TV_KILL_TIMEOUT_S}" && return 0
    log WARN "SIGTERM did not stop TV; sending SIGKILL"
    env kill -KILL "${pid}" 2>/dev/null || true
    wait_for_exit "${TV_KILL_TIMEOUT_S}"
}

launch_tv() {
    # Re-opens TV.app with the canonical CDP flag. `open -a ... --args`
    # only honors --args when the app isn't already running; callers MUST
    # ensure tv_running is false first.
    if [[ "${DRY_RUN}" == "1" ]]; then
        log INFO "DRY_RUN would: open -a ${TV_APP_PATH} --args --remote-debugging-port=${TV_CDP_PORT}"
        return 0
    fi
    open -a "${TV_APP_PATH}" --args "--remote-debugging-port=${TV_CDP_PORT}"
}

verify_cdp_recovered() {
    # Poll the canonical TV_CDP_PORT every 2s up to $TV_VERIFY_TIMEOUT_S.
    # NOTE: this only checks port reachability — chart tab presence is
    # validated on the *next* watchdog run, because TV restores its
    # workspace asynchronously and the tab may not be ready before this
    # window closes.
    local waited=0
    while (( waited < TV_VERIFY_TIMEOUT_S )); do
        if cdp_version_reachable_on "${TV_CDP_PORT}"; then
            return 0
        fi
        sleep 2
        waited=$(( waited + 2 ))
    done
    return 1
}

# ---------------------------------------------------------------------------
# State machine
# ---------------------------------------------------------------------------
main() {
    if ! acquire_lock; then
        log INFO "another instance is running (lock=${LOCK_DIR}); skipping"
        return 0
    fi

    log INFO "PROBE range=${TV_CDP_PORT_RANGE}"
    local healthy
    if healthy="$(healthy_port)"; then
        if [[ "${healthy}" == "${TV_CDP_PORT}" ]]; then
            log INFO "VALIDATE ok (port=${healthy}, tradingview tab present); healthy"
        else
            log INFO "VALIDATE ok (port=${healthy} != canonical ${TV_CDP_PORT}, tradingview tab present); accepted — data-svc scans the same range"
        fi
        return 0
    fi

    local warn_port
    if warn_port="$(any_cdp_with_no_tab)"; then
        log WARN "VALIDATE failed: CDP up on port=${warn_port} but no tradingview tab — manual intervention needed (open a chart in TV)"
        return 0
    fi

    log INFO "ACT: no healthy CDP endpoint in ${TV_CDP_PORT_RANGE}; will (re)launch on ${TV_CDP_PORT}"

    if tv_running; then
        log INFO "ACT: TV running with wrong/missing flag; graceful quit"
        if graceful_quit; then
            if wait_for_exit "${TV_QUIT_TIMEOUT_S}"; then
                log INFO "ACT: graceful quit succeeded"
            else
                log WARN "ACT: graceful quit timed out after ${TV_QUIT_TIMEOUT_S}s; force killing"
                if ! force_kill; then
                    log ERROR "ACT: failed to kill TV process; aborting"
                    return 1
                fi
                log INFO "ACT: force kill succeeded"
            fi
        else
            log WARN "ACT: graceful quit returned error; force killing"
            if ! force_kill; then
                log ERROR "ACT: failed to kill TV process; aborting"
                return 1
            fi
        fi
    else
        log INFO "ACT: TV not running; launching fresh"
    fi

    log INFO "ACT: launching TV with --remote-debugging-port=${TV_CDP_PORT}"
    if ! launch_tv; then
        log ERROR "ACT: open -a TradingView failed"
        return 1
    fi

    log INFO "VERIFY: polling CDP on ${TV_CDP_PORT} up to ${TV_VERIFY_TIMEOUT_S}s"
    if verify_cdp_recovered; then
        log INFO "VERIFY ok: TV CDP recovered on port ${TV_CDP_PORT}"
        return 0
    fi

    log ERROR "VERIFY: TV failed to expose CDP within ${TV_VERIFY_TIMEOUT_S}s; launchd will retry"
    return 1
}

main "$@"
