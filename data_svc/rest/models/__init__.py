"""Pydantic models for the REST API.

`_generated.py` is produced by `datamodel-code-generator` from
`docs/openapi.yaml` (`make codegen`). Do not edit it by hand — CI verifies
it is byte-identical to a fresh codegen run.

Re-exporting here so callers import from a stable surface even if the
underlying generator output is reshaped.
"""
from ._generated import (
    Asset,
    AssetClass,
    Bar,
    ErrorResponse,
    HealthResponse,
    HistoricalResponse,
    HTTPValidationError,
    Quote,
    SearchResponse,
    Status,
    Timeframe,
    ValidationError,
)

__all__ = [
    "Asset",
    "AssetClass",
    "Bar",
    "ErrorResponse",
    "HealthResponse",
    "HistoricalResponse",
    "HTTPValidationError",
    "Quote",
    "SearchResponse",
    "Status",
    "Timeframe",
    "ValidationError",
]
