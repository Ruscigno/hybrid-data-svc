from __future__ import annotations

from pathlib import Path

import pytest


def test_load_assets_yaml_minimal(tmp_path):
    from data_svc.services.assets_loader import load_assets_yaml

    p = tmp_path / "assets.yaml"
    p.write_text(
        """
assets:
  - symbol: BINANCE:BTCUSDT
    storage_symbol: BTC/USDT:USDT
    name: Bitcoin
    exchange: BINANCE
    currency: USD
    asset_class: CRYPTO
"""
    )
    rows = load_assets_yaml(p)
    assert len(rows) == 1
    r = rows[0]
    assert r.symbol == "BINANCE:BTCUSDT"
    assert r.storage_symbol == "BTC/USDT:USDT"
    assert r.asset_class == "CRYPTO"
    assert r.asset_subclass is None


def test_load_assets_yaml_optional_fields(tmp_path):
    from data_svc.services.assets_loader import load_assets_yaml

    p = tmp_path / "assets.yaml"
    p.write_text(
        """
assets:
  - symbol: NASDAQ:AAPL
    storage_symbol: AAPL
    name: Apple Inc.
    exchange: NASDAQ
    currency: USD
    asset_class: EQUITY
    asset_subclass: COMMON
    isin: US0378331005
    country: US
"""
    )
    [r] = load_assets_yaml(p)
    assert r.isin == "US0378331005"
    assert r.country == "US"
    assert r.asset_subclass == "COMMON"


def test_load_rejects_invalid_asset_class(tmp_path):
    from data_svc.services.assets_loader import AssetsYamlError, load_assets_yaml

    p = tmp_path / "assets.yaml"
    p.write_text(
        """
assets:
  - symbol: X:Y
    storage_symbol: Y
    name: y
    exchange: X
    currency: USD
    asset_class: BOGUS
"""
    )
    with pytest.raises(AssetsYamlError, match="asset_class"):
        load_assets_yaml(p)


def test_load_rejects_duplicates(tmp_path):
    from data_svc.services.assets_loader import AssetsYamlError, load_assets_yaml

    p = tmp_path / "assets.yaml"
    p.write_text(
        """
assets:
  - {symbol: X:Y, storage_symbol: Y, name: y, exchange: X, currency: USD, asset_class: CRYPTO}
  - {symbol: X:Y, storage_symbol: Y2, name: y2, exchange: X, currency: USD, asset_class: CRYPTO}
"""
    )
    with pytest.raises(AssetsYamlError, match="duplicate"):
        load_assets_yaml(p)


def test_sync_missing_file_lenient_returns_zero(pg_url, reset_db, tmp_path):
    from data_svc.db.assets import AssetsRepo
    from data_svc.db import postgres as pg_mod
    from data_svc.services.assets_loader import sync

    pg_mod.close_pool()
    repo = AssetsRepo(pg_url)
    n = sync(repo, tmp_path / "missing.yaml", strict=False)
    assert n == 0


def test_sync_idempotent(pg_url, reset_db, tmp_path):
    from data_svc.db.assets import AssetsRepo
    from data_svc.db import postgres as pg_mod
    from data_svc.services.assets_loader import sync

    pg_mod.close_pool()
    repo = AssetsRepo(pg_url)
    p = tmp_path / "assets.yaml"
    p.write_text(
        """
assets:
  - symbol: BINANCE:BTCUSDT
    storage_symbol: BTC/USDT:USDT
    name: Bitcoin
    exchange: BINANCE
    currency: USD
    asset_class: CRYPTO
"""
    )
    assert sync(repo, p) == 1
    # Second run upserts the same row; no error, count stays at 1.
    assert sync(repo, p) == 1
    assert repo.count() == 1


def test_repo_distributed_assets_yaml_parses(repo_root: Path):
    """The committed data_svc/assets.yaml itself must parse and validate."""
    from data_svc.services.assets_loader import load_assets_yaml

    rows = load_assets_yaml(repo_root / "data_svc" / "assets.yaml")
    assert len(rows) >= 1
    for r in rows:
        assert r.asset_class in {"EQUITY", "CRYPTO", "ETF", "FUND"}


@pytest.fixture
def repo_root() -> Path:
    return Path(__file__).resolve().parent.parent.parent
