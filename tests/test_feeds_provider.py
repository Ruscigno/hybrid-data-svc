"""Tests that polling_targets() correctly isolates feeds by provider."""
import pytest
from data_svc.db.feeds import FeedsRepo


def test_polling_targets_isolates_by_provider(pg_url, seed_feed):
    """polling_targets('yahoo') returns only yahoo feeds;
    polling_targets() (default) returns only tradingview feeds;
    neither leaks into the other.
    """
    seed_feed("AAPL", "1h", provider_symbol="AAPL", provider="yahoo", status="active")
    seed_feed("MSFT", "1h", provider_symbol="MSFT:BINANCE", provider="tradingview", status="active")

    repo = FeedsRepo(pg_url)

    yahoo_targets = repo.polling_targets("yahoo")
    tv_targets = repo.polling_targets("tradingview")
    default_targets = repo.polling_targets()  # default = tradingview

    # Yahoo targets should only contain AAPL
    yahoo_symbols = {r.storage_symbol for r in yahoo_targets}
    assert yahoo_symbols == {"AAPL"}, f"yahoo got: {yahoo_symbols}"
    assert all(r.provider == "yahoo" for r in yahoo_targets)

    # TV targets should only contain MSFT
    tv_symbols = {r.storage_symbol for r in tv_targets}
    assert tv_symbols == {"MSFT"}, f"tradingview got: {tv_symbols}"
    assert all(r.provider == "tradingview" for r in tv_targets)

    # Default (no arg) equals tradingview
    default_symbols = {r.storage_symbol for r in default_targets}
    assert default_symbols == {"MSFT"}, f"default got: {default_symbols}"
