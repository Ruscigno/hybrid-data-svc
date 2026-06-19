"""Provider identity constants — kept dependency-light (no pandas) so the
Docker healthcheck and low-level modules can import them without pulling in
the full cache layer."""
from __future__ import annotations

# Serving precedence: the first provider with data for a (symbol, timeframe)
# wins when the caller doesn't specify one.
PROVIDER_PRECEDENCE: tuple[str, ...] = ("tradingview", "yahoo")

# Default provider for writes and for unspecified-provider reads.
DEFAULT_PROVIDER: str = PROVIDER_PRECEDENCE[0]
