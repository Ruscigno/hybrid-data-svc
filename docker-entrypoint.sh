#!/bin/sh
# Chrome's CDP security policy rejects non-localhost hostnames.
# Resolve host.docker.internal to its IPv4 address at startup so the tv CLI
# can connect using an IP, which Chrome accepts.
CDP_HOST="$(python3 -c "
import socket
try:
    addrs = socket.getaddrinfo('host.docker.internal', None, socket.AF_INET)
    print(addrs[0][4][0])
except Exception:
    print('host.docker.internal')
")"
export CDP_HOST
echo "[entrypoint] CDP_HOST resolved to $CDP_HOST"
exec "$@"
