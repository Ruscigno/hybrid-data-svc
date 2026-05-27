FROM python:3.12-slim

# Node.js 20 for the tv CLI (TradingView-MCP)
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# tv CLI from the TradingView-MCP submodule.
# Apply our patches via file replacement (cleaner than sed):
#   - cdp_discover.js (new): intelligent CDP port discovery (cache + scan).
#   - connection.js (modified): uses discoverCdp() instead of hardcoded port.
#   - core/tab.js (modified): same, deletes the duplicated CDP_HOST/CDP_PORT constants.
#   - cli/router.js (modified): drain stdout before process.exit() so large
#     payloads aren't truncated when stdout is a pipe (see BUG_tv_non_json.md).
# The upstream submodule stays untouched; the patched files live in our repo
# (data_svc/patches/) and are mirrored from trading/external/tradingview-mcp
# via scripts/check-patch-parity.sh — see README for the parity contract.
COPY external/tradingview-mcp/ /opt/tradingview-mcp/
COPY data_svc/patches/cdp_discover.js /opt/tradingview-mcp/src/cdp_discover.js
COPY data_svc/patches/connection.js   /opt/tradingview-mcp/src/connection.js
COPY data_svc/patches/core_tab.js     /opt/tradingview-mcp/src/core/tab.js
COPY data_svc/patches/cli_router.js   /opt/tradingview-mcp/src/cli/router.js
RUN cd /opt/tradingview-mcp && npm install --omit=dev && npm link

# Python service
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY data_svc/ ./data_svc/
COPY scripts/ ./scripts/
COPY migrations/ ./migrations/
COPY docs/ ./docs/

# Note: gRPC Python stubs (data_svc/grpc_server/proto/*_pb2*.py) are committed
# to the repo and regenerated via `buf generate` (see Makefile target `proto`).
# No build-time codegen — the COPY above already brings them in.

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV CDP_HOST=host.docker.internal \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "data_svc"]
