from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import NamedTuple

from dotenv import load_dotenv

load_dotenv()


class Feed(NamedTuple):
    symbol: str      # DB key — ccxt/exchange format, must match bot's SYMBOL
    timeframe: str   # e.g. "1h", "4h"
    tv_symbol: str   # TradingView chart symbol, e.g. "BINANCE:BTCUSDT"


def _parse_feeds(
    feeds_env: str,
    default_symbol: str,
    default_tf: str,
    default_tv_symbol: str,
) -> list[Feed]:
    """
    Parse FEEDS env var. Each entry: db_symbol@timeframe[@tv_symbol]
    """
    if not feeds_env.strip():
        return [Feed(default_symbol, default_tf, default_tv_symbol)]
    result = []
    for entry in feeds_env.split(","):
        parts = [p.strip() for p in entry.strip().split("@")]
        if len(parts) >= 3:
            result.append(Feed(parts[0], parts[1], parts[2]))
        elif len(parts) == 2:
            result.append(Feed(parts[0], parts[1], default_tv_symbol))
        elif len(parts) == 1 and parts[0]:
            result.append(Feed(parts[0], default_tf, default_tv_symbol))
    return result or [Feed(default_symbol, default_tf, default_tv_symbol)]


@dataclass
class DataSvcConfig:
    symbol: str = "BTC/USDT:USDT"
    timeframe: str = "1h"
    tv_symbol: str = ""
    feeds: list[Feed] = field(default_factory=list)
    bars_to_fetch: int = 300
    poll_interval_s: float = 30.0
    postgres_url: str = "postgresql://datasvc:datasvc@postgres:5432/datasvc"
    tv_cli: str = "tv"
    cdp_host: str = "localhost"
    cdp_port: int = 9222
    grpc_listen: str = "0.0.0.0:50051"

    @classmethod
    def from_env(cls) -> DataSvcConfig:
        symbol = os.getenv("SYMBOL", "BTC/USDT:USDT")
        timeframe = os.getenv("TIMEFRAME", "1h")
        tv_symbol = os.getenv("TV_SYMBOL", symbol)
        feeds = _parse_feeds(os.getenv("FEEDS", ""), symbol, timeframe, tv_symbol)
        return cls(
            symbol=symbol,
            timeframe=timeframe,
            tv_symbol=tv_symbol,
            feeds=feeds,
            bars_to_fetch=int(os.getenv("BARS_TO_FETCH", "300")),
            poll_interval_s=float(os.getenv("POLL_INTERVAL_SECONDS", "30.0")),
            postgres_url=os.getenv(
                "POSTGRES_URL",
                "postgresql://datasvc:datasvc@postgres:5432/datasvc",
            ),
            tv_cli=os.getenv("TV_CLI", "tv"),
            cdp_host=os.getenv("CDP_HOST", "localhost"),
            cdp_port=int(os.getenv("CDP_PORT", "9222")),
            grpc_listen=os.getenv("GRPC_LISTEN", "0.0.0.0:50051"),
        )
