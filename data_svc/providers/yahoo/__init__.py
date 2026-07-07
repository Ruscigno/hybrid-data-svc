"""Yahoo Finance provider package.

Public API:
  - YahooSource   — Source implementation (D9)
  - YahooClient   — Low-level HTTP client (curl_cffi + adaptive limiter)
  - get_source    — Factory/registry accessor
"""

from .source import YahooSource, get_source
from .client import YahooClient

__all__ = ["YahooSource", "YahooClient", "get_source"]
