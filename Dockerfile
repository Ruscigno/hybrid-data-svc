FROM python:3.12-slim

# Node.js 20 for the tv CLI (TradingView-MCP)
RUN apt-get update && apt-get install -y --no-install-recommends curl && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y --no-install-recommends nodejs && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# tv CLI from the TradingView-MCP submodule.
# Patch CDP_HOST/CDP_PORT to read from env (so the container can resolve host.docker.internal).
COPY external/tradingview-mcp/ /opt/tradingview-mcp/
RUN sed -i \
      "s/const CDP_HOST = 'localhost';/const CDP_HOST = process.env.CDP_HOST || 'localhost';/" \
      /opt/tradingview-mcp/src/connection.js && \
    sed -i \
      "s/const CDP_PORT = 9222;/const CDP_PORT = parseInt(process.env.CDP_PORT || '9222', 10);/" \
      /opt/tradingview-mcp/src/connection.js && \
    cd /opt/tradingview-mcp && npm install --omit=dev && npm link

# Python service
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY data_svc/ ./data_svc/
COPY scripts/ ./scripts/

# Generate gRPC Python stubs from .proto (they are gitignored).
RUN python -m grpc_tools.protoc \
        -I=data_svc/grpc_server/proto \
        --python_out=data_svc/grpc_server/proto \
        --grpc_python_out=data_svc/grpc_server/proto \
        data_svc/grpc_server/proto/bars.proto

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV CDP_HOST=host.docker.internal \
    PYTHONUNBUFFERED=1

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "data_svc"]
