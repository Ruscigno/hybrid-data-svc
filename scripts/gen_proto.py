#!/usr/bin/env python3
"""
Regenerate gRPC Python stubs from the .proto definitions.

Run after editing any *.proto:
    python -m scripts.gen_proto

Stubs land alongside the proto and are gitignored. They are also generated
at Docker build time (see Dockerfile).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROTO_DIR = ROOT / "data_svc" / "grpc_server" / "proto"


def main() -> int:
    proto_files = sorted(PROTO_DIR.glob("*.proto"))
    if not proto_files:
        print(f"no .proto files found in {PROTO_DIR}", file=sys.stderr)
        return 1

    cmd = [
        sys.executable, "-m", "grpc_tools.protoc",
        f"-I={PROTO_DIR}",
        f"--python_out={PROTO_DIR}",
        f"--grpc_python_out={PROTO_DIR}",
        *[str(p) for p in proto_files],
    ]
    print("generating:", " ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
