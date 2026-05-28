"""Alias package for `data_svc.rest` — matches the module name in the spec.

The spec's compose snippet uses `python -m data_svc.rest_server`. The actual
implementation lives at `data_svc.rest`. This package re-exports the main
entry point so both module paths work.
"""
