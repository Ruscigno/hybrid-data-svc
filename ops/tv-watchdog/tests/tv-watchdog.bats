#!/usr/bin/env bats
# Tests for ops/tv-watchdog/tv-watchdog.sh
#
# Each test injects mock binaries (curl, ps, osascript, open, kill, logger)
# on $PATH so the real TradingView.app and the network are never touched.
# Timeouts are shrunk via env vars so the whole suite runs in <30s.
#
# Run with: bats ops/tv-watchdog/tests/tv-watchdog.bats

setup() {
    REPO_ROOT="$(cd "${BATS_TEST_DIRNAME}/../../.." && pwd)"
    WATCHDOG="${REPO_ROOT}/ops/tv-watchdog/tv-watchdog.sh"
    MOCKBIN="$(mktemp -d "${BATS_TMPDIR}/mockbin.XXXXXX")"
    LOCK_DIR="$(mktemp -d -u "${BATS_TMPDIR}/wdlock.XXXXXX")"
    LOG_FILE="$(mktemp -u "${BATS_TMPDIR}/wdlog.XXXXXX.log")"

    # Tiny timeouts so tests finish fast even when probing a 9-port range
    # in a loop.
    export TV_PROBE_TIMEOUT_S=1
    export TV_QUIT_TIMEOUT_S=2
    export TV_KILL_TIMEOUT_S=2
    export TV_VERIFY_TIMEOUT_S=4
    export TV_CDP_PORT_RANGE=9222-9224  # shrink range to 3 ports → faster
    export LOCK_DIR
    export LOG_FILE

    # logger mocked to no-op so test runs don't pollute the system log.
    cat > "${MOCKBIN}/logger" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "${MOCKBIN}/logger"
    export PATH="${MOCKBIN}:${PATH}"
}

teardown() {
    rm -rf "${MOCKBIN}" "${LOCK_DIR}" "${LOG_FILE}" 2>/dev/null || true
}

# --- Mock helpers -----------------------------------------------------------

# Healthy: every port responds 200 with a TV chart tab. Used as the "happy
# path" base — `healthy_port` returns the first one (9222) immediately.
mock_curl_healthy() {
    cat > "${MOCKBIN}/curl" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    *json/version*)
        case "$*" in
            *-w*'%{http_code}'*) printf '200'; exit 0 ;;
            *) printf '{"Browser":"Chrome/120"}'; exit 0 ;;
        esac
        ;;
    *json/list*)
        printf '[{"id":"abc","type":"page","url":"https://www.tradingview.com/chart/abc/"}]'
        exit 0
        ;;
esac
exit 0
EOF
    chmod +x "${MOCKBIN}/curl"
}

# Healthy on a NON-canonical port (9223). All other ports refused. Covers
# the "TV is fine on a non-canonical port — don't thrash it" case.
mock_curl_healthy_only_on() {
    local port="$1"
    cat > "${MOCKBIN}/curl" <<EOF
#!/usr/bin/env bash
url="\${*##* }"
if [[ "\$url" != *":${port}/"* ]]; then
    case "\$*" in
        *-w*'%{http_code}'*) printf '000'; exit 7 ;;
        *) exit 7 ;;
    esac
fi
case "\$*" in
    *json/version*)
        case "\$*" in
            *-w*'%{http_code}'*) printf '200'; exit 0 ;;
            *) printf '{"Browser":"Chrome/120"}'; exit 0 ;;
        esac
        ;;
    *json/list*)
        printf '[{"id":"abc","type":"page","url":"https://www.tradingview.com/chart/abc/"}]'
        exit 0
        ;;
esac
exit 0
EOF
    chmod +x "${MOCKBIN}/curl"
}

# CDP up on 9222 but /json/list has no tradingview anywhere.
mock_curl_no_tv_tab() {
    cat > "${MOCKBIN}/curl" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    *json/version*)
        case "$*" in
            *-w*'%{http_code}'*) printf '200'; exit 0 ;;
            *) printf '{"Browser":"Chrome/120"}'; exit 0 ;;
        esac
        ;;
    *json/list*)
        printf '[{"id":"abc","type":"page","url":"https://www.example.com/"}]'
        exit 0
        ;;
esac
exit 0
EOF
    chmod +x "${MOCKBIN}/curl"
}

# Refused on every port until /tmp/MOCKBIN/.relaunched exists, then 9222
# is healthy.
mock_curl_recovers_after_relaunch() {
    cat > "${MOCKBIN}/curl" <<EOF
#!/usr/bin/env bash
if [[ -e "${MOCKBIN}/.relaunched" ]]; then
    # After relaunch only port 9222 (canonical) responds.
    url="\${*##* }"
    if [[ "\$url" != *":9222/"* ]]; then
        case "\$*" in
            *-w*'%{http_code}'*) printf '000'; exit 7 ;;
            *) exit 7 ;;
        esac
    fi
    case "\$*" in
        *json/version*)
            case "\$*" in
                *-w*'%{http_code}'*) printf '200'; exit 0 ;;
                *) printf '{"Browser":"Chrome/120"}'; exit 0 ;;
            esac
            ;;
        *json/list*)
            printf '[{"id":"abc","type":"page","url":"https://www.tradingview.com/chart/abc/"}]'
            exit 0
            ;;
    esac
fi
case "\$*" in
    *-w*'%{http_code}'*) printf '000'; exit 7 ;;
    *) exit 7 ;;
esac
EOF
    chmod +x "${MOCKBIN}/curl"
}

# Every port always refused — emulates "TV totally offline and never
# recovers". Used to drive the VERIFY-timeout case.
mock_curl_always_refused() {
    cat > "${MOCKBIN}/curl" <<'EOF'
#!/usr/bin/env bash
case "$*" in
    *-w*'%{http_code}'*) printf '000'; exit 7 ;;
    *) exit 7 ;;
esac
EOF
    chmod +x "${MOCKBIN}/curl"
}

mock_ps_tv_running() {
    cat > "${MOCKBIN}/ps" <<'EOF'
#!/usr/bin/env bash
echo "12345 /Applications/TradingView.app/Contents/MacOS/TradingView"
echo "12346 /Applications/TradingView.app/Contents/Frameworks/TradingView Helper.app/Contents/MacOS/TradingView Helper (GPU)"
EOF
    chmod +x "${MOCKBIN}/ps"
}

# ps reports TV alive until ${MOCKBIN}/.quit-issued exists, then nothing.
mock_ps_tv_running_then_gone_after_quit() {
    cat > "${MOCKBIN}/ps" <<EOF
#!/usr/bin/env bash
if [[ -e "${MOCKBIN}/.quit-issued" ]]; then
    echo "12346 /Applications/TradingView.app/Contents/Frameworks/TradingView Helper.app/Contents/MacOS/TradingView Helper (GPU)"
else
    echo "12345 /Applications/TradingView.app/Contents/MacOS/TradingView"
    echo "12346 /Applications/TradingView.app/Contents/Frameworks/TradingView Helper.app/Contents/MacOS/TradingView Helper (GPU)"
fi
EOF
    chmod +x "${MOCKBIN}/ps"
}

# ps reports TV alive ONLY until SIGKILL is issued (kill -KILL ...).
# Used to drive the SIGTERM->SIGKILL escalation test.
mock_ps_tv_dies_only_on_sigkill() {
    cat > "${MOCKBIN}/ps" <<EOF
#!/usr/bin/env bash
if [[ -e "${MOCKBIN}/.sigkill-issued" ]]; then
    echo "12346 /Applications/TradingView.app/Contents/Frameworks/TradingView Helper.app/Contents/MacOS/TradingView Helper (GPU)"
else
    echo "12345 /Applications/TradingView.app/Contents/MacOS/TradingView"
    echo "12346 /Applications/TradingView.app/Contents/Frameworks/TradingView Helper.app/Contents/MacOS/TradingView Helper (GPU)"
fi
EOF
    chmod +x "${MOCKBIN}/ps"
}

mock_ps_tv_off() {
    cat > "${MOCKBIN}/ps" <<'EOF'
#!/usr/bin/env bash
echo "1 /sbin/launchd"
EOF
    chmod +x "${MOCKBIN}/ps"
}

mock_osascript_clean_quit() {
    cat > "${MOCKBIN}/osascript" <<EOF
#!/usr/bin/env bash
touch "${MOCKBIN}/.quit-issued"
exit 0
EOF
    chmod +x "${MOCKBIN}/osascript"
}

# osascript succeeds (exit 0) but the process never actually quits, e.g.
# TV has a modal blocking the shutdown. Drives the graceful-quit-timeout
# branch.
mock_osascript_accepts_but_does_nothing() {
    cat > "${MOCKBIN}/osascript" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
    chmod +x "${MOCKBIN}/osascript"
}

mock_osascript_denied() {
    cat > "${MOCKBIN}/osascript" <<'EOF'
#!/usr/bin/env bash
echo "Not authorized to send Apple events to TradingView." >&2
exit 1
EOF
    chmod +x "${MOCKBIN}/osascript"
}

mock_open_marks_relaunch() {
    cat > "${MOCKBIN}/open" <<EOF
#!/usr/bin/env bash
touch "${MOCKBIN}/.relaunched"
touch "${MOCKBIN}/.open-called"
exit 0
EOF
    chmod +x "${MOCKBIN}/open"
}

mock_open_succeeds_no_recover() {
    cat > "${MOCKBIN}/open" <<EOF
#!/usr/bin/env bash
touch "${MOCKBIN}/.open-called"
exit 0
EOF
    chmod +x "${MOCKBIN}/open"
}

mock_open_fails() {
    cat > "${MOCKBIN}/open" <<EOF
#!/usr/bin/env bash
echo "open: can't find TradingView" >&2
exit 1
EOF
    chmod +x "${MOCKBIN}/open"
}

# `env kill` is what the script calls. Mock intercepts every variant and
# records the args, plus drives the ps "TV is dead" marker.
mock_kill_term_kills() {
    cat > "${MOCKBIN}/kill" <<EOF
#!/usr/bin/env bash
echo "kill \$*" >> "${MOCKBIN}/.kill-log"
# TERM is enough to bring TV down.
touch "${MOCKBIN}/.quit-issued"
exit 0
EOF
    chmod +x "${MOCKBIN}/kill"
}

mock_kill_only_sigkill_works() {
    cat > "${MOCKBIN}/kill" <<EOF
#!/usr/bin/env bash
echo "kill \$*" >> "${MOCKBIN}/.kill-log"
if [[ "\$*" == *"-KILL"* ]]; then
    touch "${MOCKBIN}/.sigkill-issued"
fi
exit 0
EOF
    chmod +x "${MOCKBIN}/kill"
}

mock_kill_both_fail() {
    cat > "${MOCKBIN}/kill" <<EOF
#!/usr/bin/env bash
echo "kill \$*" >> "${MOCKBIN}/.kill-log"
exit 0  # records the call but never marks TV as dead
EOF
    chmod +x "${MOCKBIN}/kill"
}

# --- Tests ------------------------------------------------------------------

@test "healthy_full: CDP up on 9222 + TV tab → exit 0, healthy" {
    mock_curl_healthy
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"VALIDATE ok"* ]]
    [[ "${output}" == *"port=9222"* ]]
    [[ "${output}" == *"healthy"* ]]
}

@test "healthy_off_canonical_port: TV on 9223 → accept (no thrash)" {
    mock_curl_healthy_only_on 9223
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"port=9223"* ]]
    [[ "${output}" == *"data-svc scans the same range"* ]]
}

@test "cdp_up_no_chart: CDP up but no tradingview tab → exit 0, WARN" {
    mock_curl_no_tv_tab
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"WARN"* ]]
    [[ "${output}" == *"no tradingview tab"* ]]
}

@test "cdp_down_tv_running_quits_clean: graceful quit → exit 0, recovered" {
    mock_curl_recovers_after_relaunch
    mock_ps_tv_running_then_gone_after_quit
    mock_osascript_clean_quit
    mock_open_marks_relaunch
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"graceful quit succeeded"* ]]
    [[ "${output}" == *"VERIFY ok"* ]]
    [[ -e "${MOCKBIN}/.open-called" ]]
}

@test "cdp_down_quit_accepted_but_tv_persists: timeout → force kill, exit 0" {
    mock_curl_recovers_after_relaunch
    mock_ps_tv_running_then_gone_after_quit
    mock_osascript_accepts_but_does_nothing  # exit 0 but never sets marker
    mock_kill_term_kills                      # TERM trips the marker
    mock_open_marks_relaunch
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"graceful quit timed out"* ]]
    [[ "${output}" == *"force killing"* ]]
    [[ "${output}" == *"VERIFY ok"* ]]
    grep -q -- "-TERM 12345" "${MOCKBIN}/.kill-log"
}

@test "cdp_down_osascript_denied_kill_falls_back: stderr surfaced, exit 0" {
    mock_curl_recovers_after_relaunch
    mock_ps_tv_running_then_gone_after_quit
    mock_osascript_denied   # echoes the exact macOS Automation error
    mock_kill_term_kills
    mock_open_marks_relaunch
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"osascript quit failed"* ]]
    [[ "${output}" == *"Not authorized to send Apple events"* ]]
    [[ "${output}" == *"force killing"* ]]
    grep -q -- "-TERM 12345" "${MOCKBIN}/.kill-log"
}

@test "cdp_down_sigterm_fails_sigkill_works: SIGKILL escalation, exit 0" {
    mock_curl_recovers_after_relaunch
    mock_ps_tv_dies_only_on_sigkill   # ps stays alive until SIGKILL
    mock_osascript_accepts_but_does_nothing
    mock_kill_only_sigkill_works
    mock_open_marks_relaunch
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"SIGTERM did not stop TV; sending SIGKILL"* ]]
    [[ "${output}" == *"VERIFY ok"* ]]
    grep -q -- "-TERM 12345" "${MOCKBIN}/.kill-log"
    grep -q -- "-KILL 12345" "${MOCKBIN}/.kill-log"
}

@test "cdp_down_unkillable_aborts: both signals fail → exit 1" {
    mock_curl_always_refused
    mock_ps_tv_running             # never flips, both kills are no-op
    mock_osascript_accepts_but_does_nothing
    mock_kill_both_fail
    mock_open_succeeds_no_recover  # shouldn't be reached
    run "${WATCHDOG}"
    [[ "${status}" -eq 1 ]]
    [[ "${output}" == *"failed to kill TV process; aborting"* ]]
    # open should NOT have been called — we aborted before launch_tv.
    [[ ! -e "${MOCKBIN}/.open-called" ]]
}

@test "cdp_down_launch_fails: open returns non-zero → exit 1" {
    mock_curl_always_refused
    mock_ps_tv_off
    mock_open_fails
    run "${WATCHDOG}"
    [[ "${status}" -eq 1 ]]
    [[ "${output}" == *"open -a TradingView failed"* ]]
}

@test "cdp_down_tv_off: TV not running → launch fresh, exit 0" {
    mock_curl_recovers_after_relaunch
    mock_ps_tv_off
    mock_open_marks_relaunch
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"TV not running"* ]]
    [[ "${output}" == *"VERIFY ok"* ]]
}

@test "relaunch_verify_timeout: open OK but CDP never recovers → exit 1" {
    mock_curl_always_refused
    mock_ps_tv_off
    mock_open_succeeds_no_recover
    run "${WATCHDOG}"
    [[ "${status}" -eq 1 ]]
    [[ "${output}" == *"VERIFY"* ]]
    [[ "${output}" == *"failed to expose CDP"* ]]
}

@test "lock_busy_with_live_owner: another instance running → exit 0, no-op" {
    # A real, live PID inside the lock dir → script must back off.
    mkdir -p "${LOCK_DIR}"
    printf '%s\n' "$$" > "${LOCK_DIR}/owner.pid"  # this test process IS alive
    mock_curl_healthy
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"another instance is running"* ]]
    [[ -d "${LOCK_DIR}" ]]
}

@test "lock_stale_dead_owner_reclaimed: orphan PID file → reclaim and proceed" {
    # PID 99999 is virtually always dead. Lock should be reclaimed silently.
    mkdir -p "${LOCK_DIR}"
    printf '99999\n' > "${LOCK_DIR}/owner.pid"
    mock_curl_healthy
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"reclaiming"* ]]
    [[ "${output}" == *"VALIDATE ok"* ]]
}

@test "lock_stale_no_pid_file_reclaimed: empty lock dir past grace → reclaim and proceed" {
    mkdir -p "${LOCK_DIR}"  # no owner.pid inside
    # Force grace=0 so an empty dir is reclaimed immediately, instead of
    # waiting 2s. With the default grace, a dir without owner.pid is
    # treated as "acquirer in progress" and the script backs off —
    # see _LOCK_PID_GRACE_S in tv-watchdog.sh and the
    # lock_acquirer_race_grace_respected test below.
    mock_curl_healthy
    export _LOCK_PID_GRACE_S=0
    run "${WATCHDOG}"
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"reclaiming"* ]]
    [[ "${output}" == *"VALIDATE ok"* ]]
}

@test "lock_acquirer_race_grace_respected: empty lock dir within grace → back off" {
    # Simulates the acquire_lock race: process A did mkdir; process B
    # arrives before A writes owner.pid. New logic must treat this as
    # "live acquirer" and NOT reclaim, so A's lock is preserved.
    mkdir -p "${LOCK_DIR}"  # no owner.pid inside, freshly created
    mock_curl_healthy
    run "${WATCHDOG}"  # uses default _LOCK_PID_GRACE_S=2
    [[ "${status}" -eq 0 ]]
    [[ "${output}" == *"another instance is running"* ]]
    [[ "${output}" != *"reclaiming"* ]]
}

@test "dry_run: logs intended actions without touching processes" {
    mock_curl_always_refused
    mock_ps_tv_running
    DRY_RUN=1 run "${WATCHDOG}"
    [[ "${status}" -eq 1 ]]  # verify still fails (no actual recovery)
    [[ "${output}" == *"DRY_RUN would"* ]]
    [[ ! -e "${MOCKBIN}/.kill-log" ]]
    [[ ! -e "${MOCKBIN}/.open-called" ]]
}

@test "install_idempotent: install.sh runs twice → exits 0 both times" {
    skip_if_no_launchctl
    sandbox_home
    mock_launchctl_recording
    bash "${REPO_ROOT}/ops/tv-watchdog/install.sh" >/dev/null
    bash "${REPO_ROOT}/ops/tv-watchdog/install.sh" >/dev/null
    # Bootout should appear twice (once silent before first bootstrap,
    # once before the second).
    bootouts="$(grep -c '^bootout' "${MOCKBIN}/.launchctl-log" || true)"
    bootstraps="$(grep -c '^bootstrap' "${MOCKBIN}/.launchctl-log" || true)"
    [[ "${bootouts}" -eq 2 ]]
    [[ "${bootstraps}" -eq 2 ]]
    # Plist landed in the sandboxed HOME, not the operator's real HOME.
    [[ -f "${HOME}/Library/LaunchAgents/com.tickerbeats.tv-desktop-watchdog.plist" ]]
}

@test "uninstall_keeps_logs: uninstall.sh leaves /tmp logs alone" {
    skip_if_no_launchctl
    sandbox_home
    mock_launchctl_recording
    : > "${LOG_FILE}"  # ensure the file exists
    # Pre-seed a fake plist in the sandboxed HOME so uninstall has something
    # to remove (otherwise the assert below is vacuous).
    mkdir -p "${HOME}/Library/LaunchAgents"
    : > "${HOME}/Library/LaunchAgents/com.tickerbeats.tv-desktop-watchdog.plist"
    bash "${REPO_ROOT}/ops/tv-watchdog/uninstall.sh" >/dev/null
    [[ -f "${LOG_FILE}" ]]
    grep -q '^bootout' "${MOCKBIN}/.launchctl-log"
    # Real-HOME safety check: uninstall only touched the sandbox.
    [[ ! -f "${HOME}/Library/LaunchAgents/com.tickerbeats.tv-desktop-watchdog.plist" ]]
}

# Helpers used only by install/uninstall tests --------------------------------

# Redirect HOME to a per-test sandbox so install.sh / uninstall.sh never
# touch ~/Library/LaunchAgents on the operator's real machine. Critical:
# without this, running the suite locally on a host that already has the
# watchdog installed would overwrite + then delete the real plist while
# the operator's launchd still has it loaded.
sandbox_home() {
    export HOME="$(mktemp -d "${BATS_TMPDIR}/fakehome.XXXXXX")"
    # teardown() in this file rm's MOCKBIN/LOCK_DIR/LOG_FILE only — we
    # need to clean HOME too. Use a per-test trap chained to bats's own.
    trap 'rm -rf "${HOME}"' EXIT
}

skip_if_no_launchctl() {
    if ! command -v launchctl >/dev/null 2>&1; then
        skip "launchctl not available"
    fi
}

mock_launchctl_recording() {
    cat > "${MOCKBIN}/launchctl" <<EOF
#!/usr/bin/env bash
echo "\$@" >> "${MOCKBIN}/.launchctl-log"
exit 0
EOF
    chmod +x "${MOCKBIN}/launchctl"
}
