/**
 * Intelligent CDP port discovery for the TradingView Desktop integration.
 *
 * Mirror of data_svc/cdp_discover.py — same algorithm and the same cache file
 * format so the Python data-svc and this Node MCP server converge on the same
 * port without coordination.
 *
 * Algorithm:
 *   1. If env CDP_PORT is set, use it verbatim (operator escape hatch).
 *   2. Probe the cached port (if a cache file exists). If host:port still
 *      hosts a TradingView instance, return it (fast path).
 *   3. Scan the configured port range. The first port whose /json/list
 *      contains a tradingview tab wins.
 *   4. Write the chosen endpoint to the cache file atomically.
 *
 * Env overrides:
 *   CDP_PORT             explicit port (skip discovery)
 *   CDP_HOST             host (defaults to "localhost")
 *   TV_MCP_CACHE_FILE    cache file path override
 *   TV_MCP_PORT_RANGE    port range "low-high" (default "9222-9230")
 */

import fs from 'fs/promises';
import fssync from 'fs';
import http from 'http';
import os from 'os';
import path from 'path';

const DEFAULT_HOST = 'localhost';
const DEFAULT_PORT_RANGE = [9222, 9230];
const PROBE_TIMEOUT_MS = 500;

export class DiscoveryError extends Error {}

// ---------------------------------------------------------------------------
// Cache file helpers
// ---------------------------------------------------------------------------

function cacheFilePath() {
  if (process.env.TV_MCP_CACHE_FILE) {
    return process.env.TV_MCP_CACHE_FILE;
  }
  const base = process.env.XDG_CACHE_HOME || path.join(os.homedir(), '.cache');
  return path.join(base, 'tv-mcp', 'cdp-port.json');
}

async function readCache() {
  const p = cacheFilePath();
  try {
    const raw = await fs.readFile(p, 'utf-8');
    const data = JSON.parse(raw);
    return { host: String(data.host), port: Number(data.port) };
  } catch {
    return null;
  }
}

async function writeCache(host, port) {
  const p = cacheFilePath();
  const dir = path.dirname(p);
  try {
    await fs.mkdir(dir, { recursive: true });
  } catch (err) {
    process.stderr.write(`[cdp_discover] cannot create cache dir ${dir}: ${err.message}\n`);
    return;
  }
  const payload = {
    host,
    port,
    discovered_at: new Date().toISOString().replace(/\.\d{3}Z$/, 'Z'),
    probe_url: `http://${host}:${port}/json/version`,
  };
  // Atomic write: write to a sibling tmp file, then rename.
  const tmp = path.join(dir, `.tmp_cdp-port-${process.pid}-${Date.now()}.json`);
  try {
    await fs.writeFile(tmp, JSON.stringify(payload), 'utf-8');
    await fs.rename(tmp, p);
  } catch (err) {
    process.stderr.write(`[cdp_discover] cache write failed: ${err.message}\n`);
    try { await fs.unlink(tmp); } catch {}
  }
}

// ---------------------------------------------------------------------------
// Port probing
// ---------------------------------------------------------------------------

function httpGetJson(url, timeoutMs = PROBE_TIMEOUT_MS) {
  return new Promise(resolve => {
    let settled = false;
    const finish = (value) => { if (!settled) { settled = true; resolve(value); } };

    const req = http.get(url, { timeout: timeoutMs }, res => {
      if (res.statusCode !== 200) {
        res.resume();
        return finish(null);
      }
      const chunks = [];
      res.on('data', c => chunks.push(c));
      res.on('end', () => {
        try { finish(JSON.parse(Buffer.concat(chunks).toString('utf-8'))); }
        catch { finish(null); }
      });
      res.on('error', () => finish(null));
    });
    req.on('error', () => finish(null));
    req.on('timeout', () => { req.destroy(); finish(null); });
  });
}

async function isTvEndpoint(host, port) {
  const version = await httpGetJson(`http://${host}:${port}/json/version`);
  if (!version) return false;
  const targets = await httpGetJson(`http://${host}:${port}/json/list`);
  if (!Array.isArray(targets)) return false;
  return targets.some(t =>
    t && t.type === 'page' && /tradingview/i.test(t.url || '')
  );
}

function parsePortRange(spec) {
  if (!spec) return DEFAULT_PORT_RANGE;
  const m = String(spec).match(/^(\d+)-(\d+)$/);
  if (!m) {
    process.stderr.write(`[cdp_discover] invalid TV_MCP_PORT_RANGE=${spec} — using default\n`);
    return DEFAULT_PORT_RANGE;
  }
  const lo = Number(m[1]), hi = Number(m[2]);
  if (lo >= 1 && lo <= hi && hi <= 65535) return [lo, hi];
  process.stderr.write(`[cdp_discover] invalid TV_MCP_PORT_RANGE=${spec} — using default\n`);
  return DEFAULT_PORT_RANGE;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Resolve {host, port} for the TradingView CDP endpoint.
 * @param {{forceRefresh?: boolean}} opts forceRefresh=true skips the cache
 *   validation path (useful after a mid-session failure to force a rescan).
 *   Env CDP_PORT still wins.
 */
export async function discoverCdp(opts = {}) {
  const { forceRefresh = false } = opts;

  // 1. Explicit env override.
  if (process.env.CDP_PORT) {
    const port = Number(process.env.CDP_PORT);
    if (Number.isFinite(port) && port > 0) {
      return { host: process.env.CDP_HOST || DEFAULT_HOST, port };
    }
    process.stderr.write(`[cdp_discover] invalid CDP_PORT=${process.env.CDP_PORT} — falling back to discovery\n`);
  }

  const host = process.env.CDP_HOST || DEFAULT_HOST;

  // 2. Fast path: validate the cached port.
  if (!forceRefresh) {
    const cached = await readCache();
    if (cached) {
      const probeHost = host !== DEFAULT_HOST ? host : cached.host;
      if (await isTvEndpoint(probeHost, cached.port)) {
        process.stderr.write(`[cdp_discover] cache hit: ${probeHost}:${cached.port}\n`);
        return { host: probeHost, port: cached.port };
      }
      process.stderr.write(`[cdp_discover] cache miss at ${probeHost}:${cached.port} — scanning\n`);
    }
  }

  // 3. Scan range.
  const [lo, hi] = parsePortRange(process.env.TV_MCP_PORT_RANGE);
  const matches = [];
  for (let port = lo; port <= hi; port++) {
    if (await isTvEndpoint(host, port)) matches.push(port);
  }

  if (matches.length === 0) {
    throw new DiscoveryError(
      `No TradingView CDP endpoint found at ${host}:${lo}..${hi}. ` +
      'Ensure TradingView Desktop is running with --remote-debugging-port=<PORT> ' +
      'AND at least one chart tab is open. To bypass discovery, set CDP_PORT explicitly.'
    );
  }

  // Prefer cached port if alive; otherwise lowest match.
  const cached = !forceRefresh ? await readCache() : null;
  const chosen = (cached && matches.includes(cached.port)) ? cached.port : matches[0];

  if (matches.length > 1) {
    process.stderr.write(
      `[cdp_discover] ${matches.length} TV endpoints found at ${host} (ports=${matches.join(',')}); using ${chosen}\n`
    );
  } else {
    process.stderr.write(`[cdp_discover] scanned ${lo}-${hi}, found TV on ${host}:${chosen}\n`);
  }

  await writeCache(host, chosen);
  return { host, port: chosen };
}

/**
 * Synchronous helper for the rare case where a fully-resolved (host, port)
 * is already in the cache and the caller can't await. Returns null on miss.
 */
export function readCachedSync() {
  try {
    const raw = fssync.readFileSync(cacheFilePath(), 'utf-8');
    const data = JSON.parse(raw);
    return { host: String(data.host), port: Number(data.port) };
  } catch {
    return null;
  }
}
