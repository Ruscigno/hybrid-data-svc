"""Shared `responses=` blocks for FastAPI route decorators.

Centralized so the documented error contracts (mirrored in docs/openapi.yaml)
stay in lockstep. The drift test enforces this — if you add an error status
code in a router, declare it here too, and add it to docs/openapi.yaml.
"""

from __future__ import annotations

from .models import ErrorResponse


_unauthorized = {
    "model": ErrorResponse,
    "description": "Bearer token missing or invalid.",
}
_unknown_symbol = {
    "model": ErrorResponse,
    "description": "Symbol is not in the asset catalog.",
}
_no_data = {
    "model": ErrorResponse,
    "description": "No bars stored yet for this symbol/timeframe.",
}
_bad_range = {
    "model": ErrorResponse,
    "description": "Invalid query parameters (e.g. `from >= to`, unsupported interval).",
}


QUOTE_RESPONSES = {
    401: _unauthorized,
    404: _unknown_symbol,
    503: _no_data,
}

HISTORICAL_RESPONSES = {
    400: _bad_range,
    401: _unauthorized,
    404: _unknown_symbol,
}

SEARCH_RESPONSES = {
    401: _unauthorized,
}

PROFILE_RESPONSES = {
    401: _unauthorized,
    404: _unknown_symbol,
}
