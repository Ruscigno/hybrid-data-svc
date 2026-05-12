"""
Make this directory importable as a flat module so that generated
`bars_pb2_grpc.py`'s `import bars_pb2` lookup resolves to its sibling file.

Generated stubs are not checked into git (see .gitignore); they are produced
by `scripts/gen_proto.py` at install/build time.
"""
import os
import sys

_HERE = os.path.dirname(__file__)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)
