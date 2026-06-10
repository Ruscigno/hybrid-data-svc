# TradingView Desktop watchdog

A host-side LaunchAgent that keeps TradingView Desktop exposing CDP on
`localhost:9222` with at least one chart tab open. Eliminates the
`hybrid-data-svc` writer's crash-loop when TV is launched without the
`--remote-debugging-port` flag (e.g. via Dock or after a reboot).

See [docs/superpowers/specs/2026-06-10-tv-desktop-watchdog-design.md](../../docs/superpowers/specs/2026-06-10-tv-desktop-watchdog-design.md)
for the full design rationale.

## What it does

Every 5 minutes (and once at login):

1. **Probe** — scan `TV_CDP_PORT_RANGE` (default `9222-9230`, same range
   the data-svc writer scans). For each port: `curl /json/version` +
   `/json/list`.
2. **Validate** — first port with CDP up AND a tab whose URL contains
   `tradingview` (case-insensitive) wins. Matches the exact heuristic in
   `data_svc/cdp_discover.py:_is_tv_endpoint`.
3. **Act** — only when no port in the range is healthy: quit TV
   gracefully via `osascript`, falling back to `env kill -TERM`/`-KILL`
   if it doesn't respond, then relaunch with
   `--remote-debugging-port=9222` and wait up to 60s for CDP to come
   back.
4. **CDP up but no TV tab** → log `WARN` and exit. The watchdog never
   reopens charts on the operator's behalf.

## Files

| File | Purpose |
|---|---|
| `tv-watchdog.sh` | The script. Bash, zero deps beyond macOS defaults. |
| `com.tickerbeats.tv-desktop-watchdog.plist` | LaunchAgent template with `@WATCHDOG_SH@` placeholder. |
| `install.sh` | Renders the placeholder, copies to `~/Library/LaunchAgents/`, `bootstrap`s. Idempotent. |
| `uninstall.sh` | Symmetric. Leaves `/tmp/tv-watchdog.*` logs alone for review. |
| `tests/tv-watchdog.bats` | 17 bats cases covering the state machine, lock recovery, install/uninstall. |

## Install

Prereqs:

- macOS with TradingView.app under `/Applications/` (override with `TV_APP_PATH`).
- This repo cloned to a **stable path** — `install.sh` bakes the
  absolute path of `tv-watchdog.sh` into the LaunchAgent plist. Moving
  the repo later requires re-running `install.sh`.
- `bats-core` only if you want to run the tests (`brew install bats-core`).

```bash
./install.sh
```

The installer is idempotent — re-running it just re-loads the latest
plist + script. After a `git pull` that touches the script, you don't
need to re-install (launchd picks up the script content on each fire);
you only need to re-install if you change the plist.

### First-run permission

`osascript ... tell application "TradingView" to quit` needs the
**Automation** permission in System Settings → Privacy & Security →
Automation. Run the script once manually so the prompt appears tied to
your Terminal session, then approve:

```bash
./tv-watchdog.sh
# A dialog should appear: "Terminal wants control of TradingView" → Allow
```

If the prompt is dismissed or the permission is denied, the watchdog
still works — `osascript` will exit non-zero and the script falls back
to `kill -TERM` / `-KILL` automatically. Approving just keeps the quit
flow clean.

### Verify

```bash
launchctl list | grep tv-desktop-watchdog
tail -f /tmp/tv-watchdog.log
# Or, via Unified Logging — note `eventMessage CONTAINS` (macOS `logger`
# doesn't expose --subsystem, so subsystem-predicate queries return empty):
log show --predicate 'eventMessage CONTAINS "tv-watchdog"' --last 10m
```

The agent should be listed with a recent PID (or `-` if idle). Logs
appear with `[run=<8-hex>]` markers in **all three** destinations
(stdout, file, Unified Logging) so a single execution can be traced
end-to-end.

## Configuration (env)

All tunables have production-sane defaults; override in the plist's
`EnvironmentVariables` block if you need to.

| Var | Default | Purpose |
|---|---|---|
| `TV_CDP_PORT` | `9222` | Port passed to `--remote-debugging-port` on relaunch. |
| `TV_CDP_PORT_RANGE` | `9222-9230` | Inclusive range PROBE scans. Mirrors `cdp_discover.py` default so TV healthy on 9223+ isn't thrashed. |
| `TV_PROBE_TIMEOUT_S` | `2` | `curl --max-time` on probe + validate. |
| `TV_QUIT_TIMEOUT_S` | `10` | Wait window for graceful quit before falling back to force kill. |
| `TV_KILL_TIMEOUT_S` | `5` | Wait window after `env kill -TERM` before escalating to `-KILL`. |
| `TV_VERIFY_TIMEOUT_S` | `60` | Wait window for TV to re-expose CDP after relaunch. |
| `TV_APP_PATH` | `/Applications/TradingView.app` | Path to the .app bundle. |
| `LOCK_DIR` | `/tmp/tv-watchdog.lock.d` | Lock dir (atomic mkdir). Stores `owner.pid`. |
| `LOG_FILE` | `/tmp/tv-watchdog.log` | Append-only file log (ISO 8601 + `[run=...]`). |
| `_LOCK_MAX_AGE_S` | `180` | Stale-lock reclaim threshold. `_` prefix = internal; touch only if you know why. |
| `DRY_RUN` | `0` | When `1`, the script logs intended actions but executes nothing destructive. |

## Smoke test

With a healthy stack:

```bash
./tv-watchdog.sh ; echo "exit=$?"
# Expected: exit=0; logs contain "VALIDATE ok (chart tab present); healthy"
```

Dry-run with TV intentionally misconfigured (or stopped):

```bash
DRY_RUN=1 ./tv-watchdog.sh ; echo "exit=$?"
# Expected: logs show "DRY_RUN would: osascript quit", "DRY_RUN would: open -a ..."
# No actual processes touched.
```

## Tests

```bash
brew install bats-core   # one-off
bats ops/tv-watchdog/tests/tv-watchdog.bats
# Expected: 17 tests, all passing, ~10s.
```

The bats suite injects mock `curl`/`ps`/`osascript`/`open`/`kill`/`logger`/`launchctl`
binaries via `PATH` so it never touches the real TradingView.app, the network,
or the system LaunchAgent registry.

## Uninstall

```bash
./uninstall.sh
```

Removes the LaunchAgent. Logs in `/tmp/tv-watchdog.{log,launchd.out,launchd.err}`
stay for review.

## Log rotation

Nothing is rotated automatically. Typical volume is ~50 lines/day, so the
files are negligible in practice; but on a machine that stays up for
months, run `truncate -s 0 /tmp/tv-watchdog.log /tmp/tv-watchdog.launchd.{out,err}`
when convenient.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `another instance is running; skipping` repeatedly with no `reclaiming` line | A previous run hung within the 180s stale threshold. | Wait ≤180s — the next cycle will reclaim automatically. Or `rm -rf /tmp/tv-watchdog.lock.d` and run again. |
| `osascript quit failed` every cycle, stderr says "Not authorized to send Apple events" | Automation permission denied. | Settings → Privacy & Security → Automation → grant Terminal control of TradingView. Or accept the SIGTERM fallback (also works, just less clean). |
| `VERIFY ... failed to expose CDP` | TV update in progress, or the app is hung at a dialog. | Check `/Applications/TradingView.app` manually. Watchdog will retry on the next cycle. |
| Watchdog never fires | Plist not loaded. | `launchctl list \| grep tv-desktop-watchdog`. Re-run `./install.sh`. |
| Watchdog kills TV even though TV was working | You overrode `--remote-debugging-port` to a value outside `TV_CDP_PORT_RANGE`. | Add that port to `TV_CDP_PORT_RANGE` in the plist's `EnvironmentVariables`, or rerun TV on a port within the range. |
