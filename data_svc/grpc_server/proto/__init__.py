"""
Make this directory importable as a flat module so that generated
`bars_pb2_grpc.py`'s `import bars_pb2` lookup resolves to its sibling file.

The buf-emitted grpc-python plugin (like the legacy grpc_tools.protoc plugin
it replaced) writes a flat `import bars_pb2` statement that only resolves
when `proto/` is on sys.path. Inserting it here keeps callers oblivious.

Stubs ARE committed to the repo and regenerated via `buf generate`
(see `make proto`). CI runs the regen + diff as a drift gate.
"""
import os
import sys

_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
