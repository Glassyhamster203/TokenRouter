"""Synchronise users declared in JSON/TOML into the SQLite key table."""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from .config import Config
from .db import Database, DatabaseError

logger = logging.getLogger("token_router.config")


@dataclass(frozen=True)
class UserSyncSummary:
    created: int = 0
    updated: int = 0
    skipped_revoked: int = 0


def sync_config_users(config: Config, db: Database) -> UserSyncSummary:
    """Upsert file-declared users by name.

    Users absent from the file are intentionally left untouched, so CLI-created
    keys and historical data are never deleted by a config reload.
    """

    created = 0
    updated = 0
    skipped = 0
    for user in config.users:
        existing = db.get_key_by_name(user.name)
        try:
            if existing is None:
                key = db.create_key(
                    name=user.name,
                    api_key=user.api_key,
                    price_in_micros_per_mtok=config.default_price_in_micros_per_mtok,
                    price_out_micros_per_mtok=config.default_price_out_micros_per_mtok,
                    daily_token_limit=user.daily_token_limit,
                    monthly_token_limit=user.monthly_token_limit,
                    daily_cost_limit_micros=user.daily_cost_limit_micros,
                    monthly_cost_limit_micros=user.monthly_cost_limit_micros,
                    note=user.note,
                    allowed_models=user.allowed_models,
                )
                if not user.enabled:
                    db.set_enabled(key.id, False)
                created += 1
                logger.info("config user created: %s (#%s)", user.name, key.id)
                continue
            if existing.revoked_at is not None:
                skipped += 1
                logger.warning(
                    "config user %s matches revoked key #%s; leaving it revoked",
                    user.name,
                    existing.id,
                )
                continue
            db.sync_key_from_config(
                key_id=existing.id,
                name=user.name,
                api_key=user.api_key,
                daily_token_limit=user.daily_token_limit,
                monthly_token_limit=user.monthly_token_limit,
                daily_cost_limit_micros=user.daily_cost_limit_micros,
                monthly_cost_limit_micros=user.monthly_cost_limit_micros,
                allowed_models=user.allowed_models,
                enabled=user.enabled,
                note=user.note,
            )
            updated += 1
            logger.info("config user updated: %s (#%s)", user.name, existing.id)
        except sqlite3.IntegrityError as exc:
            raise DatabaseError(
                f"配置用户 {user.name!r} 的 api_key 与现有 key 冲突，无法同步"
            ) from exc
    return UserSyncSummary(created=created, updated=updated, skipped_revoked=skipped)
