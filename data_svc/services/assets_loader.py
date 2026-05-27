"""Read data_svc/assets.yaml and upsert rows into the assets table.

Called at REST app startup (lifespan). Upsert-only — rows present in the
DB but missing from the YAML are NOT deleted. This is a deliberate
safety choice: a typo or accidental truncation of assets.yaml should
not nuke the catalog on next restart.

If `assets.yaml` is missing, the loader logs a warning and exits cleanly
with 0 rows loaded; the gateway still starts (it just returns 404 for
every symbol until the file is added).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable, Optional

import yaml

from ..db.assets import AssetRow, AssetsRepo

logger = logging.getLogger(__name__)


_VALID_ASSET_CLASSES = {"EQUITY", "CRYPTO", "ETF", "FUND"}


class AssetsYamlError(ValueError):
    """Raised when assets.yaml is malformed."""


def _parse_entries(raw: dict) -> list[AssetRow]:
    entries = raw.get("assets")
    if entries is None:
        raise AssetsYamlError("missing top-level `assets:` key")
    if not isinstance(entries, list):
        raise AssetsYamlError("`assets:` must be a list")

    parsed: list[AssetRow] = []
    seen: set[str] = set()
    for i, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise AssetsYamlError(f"entry #{i} must be a mapping, got {type(entry).__name__}")
        try:
            symbol = entry["symbol"]
            storage_symbol = entry["storage_symbol"]
            name = entry["name"]
            exchange = entry["exchange"]
            currency = entry["currency"]
            asset_class = entry["asset_class"]
        except KeyError as exc:
            raise AssetsYamlError(f"entry #{i} missing required key: {exc.args[0]}") from None

        if not isinstance(symbol, str) or symbol != symbol.upper():
            raise AssetsYamlError(f"entry #{i}: symbol must be uppercase string, got {symbol!r}")
        if asset_class not in _VALID_ASSET_CLASSES:
            raise AssetsYamlError(
                f"entry #{i}: asset_class must be one of {_VALID_ASSET_CLASSES}, got {asset_class!r}"
            )
        if symbol in seen:
            raise AssetsYamlError(f"entry #{i}: duplicate symbol {symbol!r}")
        seen.add(symbol)

        parsed.append(
            AssetRow(
                symbol=symbol,
                storage_symbol=storage_symbol,
                name=name,
                exchange=exchange,
                currency=currency,
                asset_class=asset_class,
                asset_subclass=entry.get("asset_subclass"),
                isin=entry.get("isin"),
                country=entry.get("country"),
            )
        )
    return parsed


def load_assets_yaml(path: Path) -> list[AssetRow]:
    """Read and validate the YAML. Raises AssetsYamlError on malformed input."""
    raw = yaml.safe_load(path.read_text())
    if raw is None:
        return []
    if not isinstance(raw, dict):
        raise AssetsYamlError("top-level must be a mapping")
    return _parse_entries(raw)


def sync(
    repo: AssetsRepo,
    yaml_path: Path,
    *,
    strict: bool = False,
) -> int:
    """Read assets.yaml and upsert into the catalog.

    Returns the number of rows upserted. If yaml_path is missing:
      * strict=True  → raises FileNotFoundError.
      * strict=False → logs a warning and returns 0 (default; the gateway
        can still serve /healthz and 404 every other route).
    """
    if not yaml_path.exists():
        msg = f"assets.yaml not found at {yaml_path}"
        if strict:
            raise FileNotFoundError(msg)
        logger.warning("%s — assets catalog will be empty", msg)
        return 0

    rows = load_assets_yaml(yaml_path)
    written = repo.upsert_many(rows)
    logger.info("[assets-loader] upserted %d row(s) from %s", written, yaml_path)
    return written
