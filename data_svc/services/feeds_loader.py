"""Seed the `feeds` table from the FEEDS env at data-svc startup.

Mirrors the assets_loader pattern: idempotent, never raises (we log and
move on if the DB is unreachable so the writer's own retry logic decides
when to die). Called once per process — the writer's poll loop then
re-reads `feeds` from Postgres each cycle and discovers any rows added
via POST /v1/assets without restart.
"""

from __future__ import annotations

import logging
from typing import Iterable

from ..config import Feed
from ..db.feeds import FeedsRepo

logger = logging.getLogger(__name__)


def sync(repo: FeedsRepo, env_feeds: Iterable[Feed]) -> int:
    """Upsert the env-configured feeds. Returns the number of new rows
    actually inserted (zero on subsequent runs / restarts).

    Errors are logged at ERROR but never propagated — the writer's loop
    will retry from the DB on its next cycle.
    """
    try:
        return repo.seed_from_env(list(env_feeds))
    except Exception as exc:
        logger.error("[feeds-loader] seed_from_env failed: %s", exc)
        return 0
