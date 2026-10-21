"""SQLite storage: virtual keys, quota buckets and an auditable request ledger."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

from .usage import TokenUsage
from .util import (
    PeriodBounds,
    UserError,
    api_key_prefix,
    hash_api_key,
    period_bounds,
)

SCHEMA_VERSION = 2


class DatabaseError(RuntimeError):
    pass


@dataclass(frozen=True)
class ApiKey:
    id: int
    name: str
    key_prefix: str
    enabled: bool
    revoked_at: int | None
    created_at: int
    last_used_at: int | None
    price_in_micros_per_mtok: int
    price_out_micros_per_mtok: int
    daily_token_limit: int | None
    monthly_token_limit: int | None
    daily_cost_limit_micros: int | None
    monthly_cost_limit_micros: int | None
    note: str

    @property
    def active(self) -> bool:
        return self.enabled and self.revoked_at is None

    def token_limit(self, period: str) -> int | None:
        return self.daily_token_limit if period == "day" else self.monthly_token_limit

    def cost_limit(self, period: str) -> int | None:
        return self.daily_cost_limit_micros if period == "day" else self.monthly_cost_limit_micros


@dataclass(frozen=True)
class ModelPrice:
    key_id: int | None
    model: str
    input_micros_per_mtok: int
    output_micros_per_mtok: int
    cached_input_micros_per_mtok: int | None
    updated_at: int

    @property
    def effective_cached_input_micros_per_mtok(self) -> int:
        return (
            self.cached_input_micros_per_mtok
            if self.cached_input_micros_per_mtok is not None
            else self.input_micros_per_mtok
        )

    @property
    def scope(self) -> str:
        return "global" if self.key_id is None else "key"


@dataclass(frozen=True)
class ResolvedPrice:
    input_micros_per_mtok: int
    output_micros_per_mtok: int
    cached_input_micros_per_mtok: int
    source: str
    model: str | None = None

    @classmethod
    def from_key(cls, key: ApiKey, model: str | None = None) -> "ResolvedPrice":
        return cls(
            input_micros_per_mtok=key.price_in_micros_per_mtok,
            output_micros_per_mtok=key.price_out_micros_per_mtok,
            cached_input_micros_per_mtok=key.price_in_micros_per_mtok,
            source="key-default",
            model=model,
        )


@dataclass(frozen=True)
class UsageTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0
    cost_micros: int = 0
    cached_input_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cached_input_tokens)


EMPTY_TOTALS = UsageTotals()


@dataclass(frozen=True)
class RequestLogEntry:
    id: int
    request_id: str
    key_id: int | None
    key_name: str
    ts: int
    endpoint: str
    model: str | None
    status: int
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    cost_micros: int
    estimated: bool
    latency_ms: int
    client_ip: str | None
    error: str | None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class KeyUsageOverview:
    key: ApiKey
    day: UsageTotals
    month: UsageTotals


_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_hash TEXT NOT NULL UNIQUE,
    key_prefix TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
    revoked_at INTEGER,
    created_at INTEGER NOT NULL,
    last_used_at INTEGER,
    price_in_micros_per_mtok INTEGER NOT NULL DEFAULT 0 CHECK (price_in_micros_per_mtok >= 0),
    price_out_micros_per_mtok INTEGER NOT NULL DEFAULT 0 CHECK (price_out_micros_per_mtok >= 0),
    daily_token_limit INTEGER CHECK (daily_token_limit IS NULL OR daily_token_limit >= 0),
    monthly_token_limit INTEGER CHECK (monthly_token_limit IS NULL OR monthly_token_limit >= 0),
    daily_cost_limit_micros INTEGER CHECK (daily_cost_limit_micros IS NULL OR daily_cost_limit_micros >= 0),
    monthly_cost_limit_micros INTEGER CHECK (monthly_cost_limit_micros IS NULL OR monthly_cost_limit_micros >= 0),
    note TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_api_keys_prefix ON api_keys(key_prefix);
CREATE INDEX IF NOT EXISTS idx_api_keys_active ON api_keys(enabled, revoked_at);

CREATE TABLE IF NOT EXISTS usage_buckets (
    key_id INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
    period TEXT NOT NULL CHECK (period IN ('day', 'month')),
    period_start INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    requests INTEGER NOT NULL DEFAULT 0 CHECK (requests >= 0),
    cost_micros INTEGER NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    PRIMARY KEY (key_id, period, period_start)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS request_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL UNIQUE,
    key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
    key_name TEXT NOT NULL,
    ts INTEGER NOT NULL,
    endpoint TEXT NOT NULL,
    model TEXT,
    status INTEGER NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    cost_micros INTEGER NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    estimated INTEGER NOT NULL DEFAULT 0 CHECK (estimated IN (0, 1)),
    latency_ms INTEGER NOT NULL DEFAULT 0 CHECK (latency_ms >= 0),
    client_ip TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_request_log_ts ON request_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_request_log_key_ts ON request_log(key_id, ts DESC);

CREATE TABLE IF NOT EXISTS model_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key_id INTEGER REFERENCES api_keys(id) ON DELETE CASCADE,
    model TEXT NOT NULL CHECK (length(trim(model)) > 0),
    input_micros_per_mtok INTEGER NOT NULL CHECK (input_micros_per_mtok >= 0),
    output_micros_per_mtok INTEGER NOT NULL CHECK (output_micros_per_mtok >= 0),
    cached_input_micros_per_mtok INTEGER
        CHECK (cached_input_micros_per_mtok IS NULL OR cached_input_micros_per_mtok >= 0),
    updated_at INTEGER NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_model_prices_scope
    ON model_prices(COALESCE(key_id, 0), model);
CREATE INDEX IF NOT EXISTS idx_model_prices_model ON model_prices(model);
"""


def _migrate_1_to_2(conn: sqlite3.Connection) -> None:
    """Add cache-aware columns and the model price catalog to a v1 database."""

    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in ("usage_buckets", "request_log"):
            columns = {
                str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            if "cached_input_tokens" not in columns:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN cached_input_tokens "
                    "INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0)"
                )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS model_prices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key_id INTEGER REFERENCES api_keys(id) ON DELETE CASCADE,
                model TEXT NOT NULL CHECK (length(trim(model)) > 0),
                input_micros_per_mtok INTEGER NOT NULL CHECK (input_micros_per_mtok >= 0),
                output_micros_per_mtok INTEGER NOT NULL CHECK (output_micros_per_mtok >= 0),
                cached_input_micros_per_mtok INTEGER
                    CHECK (cached_input_micros_per_mtok IS NULL OR cached_input_micros_per_mtok >= 0),
                updated_at INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_model_prices_scope "
            "ON model_prices(COALESCE(key_id, 0), model)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_model_prices_model ON model_prices(model)"
        )
        conn.execute("PRAGMA user_version=2")
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise


class Database:
    def __init__(self, path: str | Path, utc_offset_minutes: int = 480) -> None:
        self.path = Path(path)
        self.utc_offset_minutes = utc_offset_minutes
        self._local = threading.local()
        self._write_lock = threading.Lock()

    # ---------------------------------------------------------------- lifecycle
    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            version = int(conn.execute("PRAGMA user_version").fetchone()[0])
            if version > SCHEMA_VERSION:
                raise DatabaseError(
                    f"数据库 schema 版本 {version} 高于此程序支持的 {SCHEMA_VERSION}，请升级程序"
                )
            if version == 0:
                conn.executescript(_SCHEMA)
                conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            elif version == 1:
                _migrate_1_to_2(conn)
            elif version < SCHEMA_VERSION:
                raise DatabaseError(f"数据库 schema 版本过旧：{version}")
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            if not self.path.parent.exists():
                self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            self._local.conn = conn
        return conn

    def close_thread_connection(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.close()
            finally:
                self._local.conn = None

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        # SQLite permits only one writer. Serialising writers inside the process
        # avoids avoidable busy waits; WAL keeps readers unblocked.
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            else:
                try:
                    conn.execute("COMMIT")
                except BaseException:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    raise

    # --------------------------------------------------------------------- keys
    def create_key(
        self,
        *,
        name: str,
        api_key: str,
        price_in_micros_per_mtok: int,
        price_out_micros_per_mtok: int,
        daily_token_limit: int | None = None,
        monthly_token_limit: int | None = None,
        daily_cost_limit_micros: int | None = None,
        monthly_cost_limit_micros: int | None = None,
        note: str = "",
        now: int | None = None,
    ) -> ApiKey:
        clean_name = _clean_name(name)
        clean_note = _clean_note(note)
        _validate_key_limits(
            price_in_micros_per_mtok,
            price_out_micros_per_mtok,
            daily_token_limit,
            monthly_token_limit,
            daily_cost_limit_micros,
            monthly_cost_limit_micros,
        )
        timestamp = int(now if now is not None else time.time())
        with self._transaction() as conn:
            cursor = conn.execute(
                """
                INSERT INTO api_keys (
                    name, key_hash, key_prefix, enabled, created_at,
                    price_in_micros_per_mtok, price_out_micros_per_mtok,
                    daily_token_limit, monthly_token_limit,
                    daily_cost_limit_micros, monthly_cost_limit_micros, note
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    clean_name,
                    hash_api_key(api_key),
                    api_key_prefix(api_key),
                    timestamp,
                    price_in_micros_per_mtok,
                    price_out_micros_per_mtok,
                    daily_token_limit,
                    monthly_token_limit,
                    daily_cost_limit_micros,
                    monthly_cost_limit_micros,
                    clean_note,
                ),
            )
            key_id = int(cursor.lastrowid)
        return self.get_key(key_id)

    def get_key(self, key_id: int) -> ApiKey:
        row = self._connect().execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        if row is None:
            raise UserError(f"key #{key_id} 不存在")
        return _key_from_row(row)

    def get_key_by_hash(self, key_hash: str) -> ApiKey | None:
        row = self._connect().execute(
            "SELECT * FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
            (key_hash,),
        ).fetchone()
        return _key_from_row(row) if row is not None else None

    def list_keys(self, *, include_revoked: bool = False) -> list[ApiKey]:
        sql = "SELECT * FROM api_keys"
        if not include_revoked:
            sql += " WHERE revoked_at IS NULL"
        sql += " ORDER BY id"
        rows = self._connect().execute(sql).fetchall()
        return [_key_from_row(row) for row in rows]

    def resolve_key(self, reference: str) -> ApiKey:
        ref = reference.strip()
        if not ref:
            raise UserError("请提供 key ID 或前缀")
        if ref.isdigit():
            return self.get_key(int(ref))
        rows = self._connect().execute(
            "SELECT * FROM api_keys WHERE key_prefix LIKE ? ORDER BY id LIMIT 20",
            (ref + "%",),
        ).fetchall()
        if not rows:
            raise UserError(f"找不到前缀为 {ref!r} 的 key")
        if len(rows) > 1:
            ids = ", ".join(str(row["id"]) for row in rows)
            raise UserError(f"前缀 {ref!r} 匹配多个 key（ID: {ids}），请改用 ID")
        return _key_from_row(rows[0])

    def update_key(self, key_id: int, **changes: Any) -> ApiKey:
        allowed = {
            "name",
            "price_in_micros_per_mtok",
            "price_out_micros_per_mtok",
            "daily_token_limit",
            "monthly_token_limit",
            "daily_cost_limit_micros",
            "monthly_cost_limit_micros",
            "note",
            "enabled",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise DatabaseError(f"不允许更新字段：{', '.join(sorted(unknown))}")
        if not changes:
            return self.get_key(key_id)
        current = self.get_key(key_id)
        if "name" in changes:
            changes["name"] = _clean_name(str(changes["name"]))
        if "note" in changes:
            changes["note"] = _clean_note(str(changes["note"]))
        if "enabled" in changes:
            changes["enabled"] = 1 if changes["enabled"] else 0
        for field_name in (
            "price_in_micros_per_mtok",
            "price_out_micros_per_mtok",
            "daily_token_limit",
            "monthly_token_limit",
            "daily_cost_limit_micros",
            "monthly_cost_limit_micros",
        ):
            if field_name in changes:
                value = changes[field_name]
                if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
                    raise UserError(f"{field_name} 必须是非负整数或 null")
        _validate_key_limits(
            changes.get("price_in_micros_per_mtok", current.price_in_micros_per_mtok),
            changes.get("price_out_micros_per_mtok", current.price_out_micros_per_mtok),
            changes.get("daily_token_limit", current.daily_token_limit),
            changes.get("monthly_token_limit", current.monthly_token_limit),
            changes.get("daily_cost_limit_micros", current.daily_cost_limit_micros),
            changes.get("monthly_cost_limit_micros", current.monthly_cost_limit_micros),
        )
        assignments = ", ".join(f"{field_name} = ?" for field_name in changes)
        values = list(changes.values()) + [key_id]
        with self._transaction() as conn:
            cursor = conn.execute(f"UPDATE api_keys SET {assignments} WHERE id = ?", values)
            if cursor.rowcount == 0:
                raise UserError(f"key #{key_id} 不存在")
        return self.get_key(key_id)

    def set_enabled(self, key_id: int, enabled: bool) -> ApiKey:
        return self.update_key(key_id, enabled=enabled)

    def revoke_key(self, key_id: int, *, now: int | None = None) -> ApiKey:
        timestamp = int(now if now is not None else time.time())
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE api_keys SET enabled = 0, revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
                (timestamp, key_id),
            )
            if cursor.rowcount == 0:
                raise UserError(f"key #{key_id} 不存在或已经吊销")
        return self.get_key(key_id)

    def rotate_key(self, key_id: int, new_api_key: str) -> ApiKey:
        with self._transaction() as conn:
            cursor = conn.execute(
                "UPDATE api_keys SET key_hash = ?, key_prefix = ?, enabled = 1, revoked_at = NULL WHERE id = ?",
                (hash_api_key(new_api_key), api_key_prefix(new_api_key), key_id),
            )
            if cursor.rowcount == 0:
                raise UserError(f"key #{key_id} 不存在")
        return self.get_key(key_id)

    # ------------------------------------------------------------------- prices
    def set_model_price(
        self,
        *,
        model: str,
        input_micros_per_mtok: int,
        output_micros_per_mtok: int,
        cached_input_micros_per_mtok: int | None = None,
        key_id: int | None = None,
        now: int | None = None,
    ) -> ModelPrice:
        clean_model = _clean_model(model)
        _validate_key_limits(
            input_micros_per_mtok,
            output_micros_per_mtok,
            cached_input_micros_per_mtok,
        )
        if key_id is not None:
            self.get_key(key_id)
        timestamp = int(now if now is not None else time.time())
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO model_prices (
                    key_id, model, input_micros_per_mtok, output_micros_per_mtok,
                    cached_input_micros_per_mtok, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT DO UPDATE SET
                    input_micros_per_mtok = excluded.input_micros_per_mtok,
                    output_micros_per_mtok = excluded.output_micros_per_mtok,
                    cached_input_micros_per_mtok = excluded.cached_input_micros_per_mtok,
                    updated_at = excluded.updated_at
                """,
                (
                    key_id,
                    clean_model,
                    input_micros_per_mtok,
                    output_micros_per_mtok,
                    cached_input_micros_per_mtok,
                    timestamp,
                ),
            )
        price = self.get_model_price(clean_model, key_id=key_id)
        assert price is not None
        return price

    def get_model_price(self, model: str, *, key_id: int | None = None) -> ModelPrice | None:
        row = self._connect().execute(
            "SELECT * FROM model_prices WHERE model = ? AND key_id IS ?",
            (_clean_model(model), key_id),
        ).fetchone()
        return _model_price_from_row(row) if row is not None else None

    def list_model_prices(
        self,
        *,
        key_id: int | None = None,
        include_global: bool = True,
    ) -> list[ModelPrice]:
        if key_id is None:
            rows = self._connect().execute(
                "SELECT * FROM model_prices WHERE key_id IS NULL ORDER BY model"
            ).fetchall()
        elif include_global:
            rows = self._connect().execute(
                """
                SELECT * FROM model_prices
                WHERE key_id = ? OR key_id IS NULL
                ORDER BY model, (key_id IS NULL)
                """,
                (key_id,),
            ).fetchall()
        else:
            rows = self._connect().execute(
                "SELECT * FROM model_prices WHERE key_id = ? ORDER BY model",
                (key_id,),
            ).fetchall()
        return [_model_price_from_row(row) for row in rows]

    def remove_model_price(self, model: str, *, key_id: int | None = None) -> bool:
        with self._transaction() as conn:
            cursor = conn.execute(
                "DELETE FROM model_prices WHERE model = ? AND key_id IS ?",
                (_clean_model(model), key_id),
            )
            return cursor.rowcount > 0

    def resolve_price(self, key: ApiKey, model: str | None) -> ResolvedPrice:
        """Resolve key-specific model price, then global model price, then key default."""

        if model:
            clean_model = _clean_model(model)
            row = self._connect().execute(
                """
                SELECT * FROM model_prices
                WHERE model = ? AND (key_id = ? OR key_id IS NULL)
                ORDER BY (key_id IS NULL) ASC
                LIMIT 1
                """,
                (clean_model, key.id),
            ).fetchone()
            if row is not None:
                price = _model_price_from_row(row)
                return ResolvedPrice(
                    input_micros_per_mtok=price.input_micros_per_mtok,
                    output_micros_per_mtok=price.output_micros_per_mtok,
                    cached_input_micros_per_mtok=price.effective_cached_input_micros_per_mtok,
                    source="key-model" if price.key_id is not None else "global-model",
                    model=clean_model,
                )
        return ResolvedPrice.from_key(key, model)

    # -------------------------------------------------------------------- usage
    def get_bucket(self, key_id: int, period: str, period_start: int) -> UsageTotals:
        row = self._connect().execute(
            """
            SELECT input_tokens, output_tokens, requests, cost_micros, cached_input_tokens
            FROM usage_buckets
            WHERE key_id = ? AND period = ? AND period_start = ?
            """,
            (key_id, period, period_start),
        ).fetchone()
        return _totals_from_row(row)

    def get_period_usage(self, key_id: int, bounds: PeriodBounds) -> tuple[UsageTotals, UsageTotals]:
        return (
            self.get_bucket(key_id, "day", bounds.day_start),
            self.get_bucket(key_id, "month", bounds.month_start),
        )

    def usage_overview(self, bounds: PeriodBounds, *, include_revoked: bool = False) -> list[KeyUsageOverview]:
        where = "" if include_revoked else "WHERE k.revoked_at IS NULL"
        rows = self._connect().execute(
            f"""
            SELECT
                k.*,
                COALESCE(d.input_tokens, 0) AS d_input_tokens,
                COALESCE(d.output_tokens, 0) AS d_output_tokens,
                COALESCE(d.requests, 0) AS d_requests,
                COALESCE(d.cost_micros, 0) AS d_cost_micros,
                COALESCE(d.cached_input_tokens, 0) AS d_cached_input_tokens,
                COALESCE(m.input_tokens, 0) AS m_input_tokens,
                COALESCE(m.output_tokens, 0) AS m_output_tokens,
                COALESCE(m.requests, 0) AS m_requests,
                COALESCE(m.cost_micros, 0) AS m_cost_micros,
                COALESCE(m.cached_input_tokens, 0) AS m_cached_input_tokens
            FROM api_keys k
            LEFT JOIN usage_buckets d
                ON d.key_id = k.id AND d.period = 'day' AND d.period_start = ?
            LEFT JOIN usage_buckets m
                ON m.key_id = k.id AND m.period = 'month' AND m.period_start = ?
            {where}
            ORDER BY k.id
            """,
            (bounds.day_start, bounds.month_start),
        ).fetchall()
        result: list[KeyUsageOverview] = []
        for row in rows:
            day = UsageTotals(
                int(row["d_input_tokens"]),
                int(row["d_output_tokens"]),
                int(row["d_requests"]),
                int(row["d_cost_micros"]),
                int(row["d_cached_input_tokens"]),
            )
            month = UsageTotals(
                int(row["m_input_tokens"]),
                int(row["m_output_tokens"]),
                int(row["m_requests"]),
                int(row["m_cost_micros"]),
                int(row["m_cached_input_tokens"]),
            )
            result.append(KeyUsageOverview(_key_from_row(row), day, month))
        return result

    def record_request(
        self,
        *,
        request_id: str,
        key: ApiKey,
        timestamp: int,
        endpoint: str,
        model: str | None,
        status: int,
        usage: TokenUsage,
        cost_micros: int,
        latency_ms: int,
        client_ip: str | None,
        error: str | None = None,
        bounds: PeriodBounds | None = None,
    ) -> None:
        if cost_micros < 0 or usage.input_tokens < 0 or usage.output_tokens < 0:
            raise DatabaseError("usage/cost 不能为负数")
        cached_input_tokens = min(max(0, usage.cached_input_tokens), usage.input_tokens)
        period = bounds or period_bounds(timestamp, self.utc_offset_minutes)
        bounded_error = (error or "")[:500] or None
        metered = status >= 200 and status < 300 and (usage.total_tokens > 0 or cost_micros > 0)
        with self._transaction() as conn:
            conn.execute(
                """
                INSERT INTO request_log (
                    request_id, key_id, key_name, ts, endpoint, model, status,
                    input_tokens, output_tokens, cost_micros, cached_input_tokens,
                    estimated, latency_ms, client_ip, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request_id,
                    key.id,
                    key.name,
                    int(timestamp),
                    endpoint,
                    model,
                    int(status),
                    int(usage.input_tokens),
                    int(usage.output_tokens),
                    int(cost_micros),
                    int(cached_input_tokens),
                    1 if usage.estimated else 0,
                    int(max(0, latency_ms)),
                    client_ip,
                    bounded_error,
                ),
            )
            if metered:
                for period_name, period_start in (("day", period.day_start), ("month", period.month_start)):
                    conn.execute(
                        """
                        INSERT INTO usage_buckets (
                            key_id, period, period_start, input_tokens,
                            output_tokens, requests, cost_micros, cached_input_tokens
                        ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                        ON CONFLICT(key_id, period, period_start) DO UPDATE SET
                            input_tokens = input_tokens + excluded.input_tokens,
                            output_tokens = output_tokens + excluded.output_tokens,
                            requests = requests + 1,
                            cost_micros = cost_micros + excluded.cost_micros,
                            cached_input_tokens = cached_input_tokens + excluded.cached_input_tokens
                        """,
                        (
                            key.id,
                            period_name,
                            period_start,
                            int(usage.input_tokens),
                            int(usage.output_tokens),
                            int(cost_micros),
                            int(cached_input_tokens),
                        ),
                    )
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ? AND (last_used_at IS NULL OR last_used_at < ?)",
                (int(timestamp), key.id, int(timestamp)),
            )

    def recent_requests(
        self,
        *,
        limit: int = 30,
        key_id: int | None = None,
        since: int | None = None,
    ) -> list[RequestLogEntry]:
        clauses: list[str] = []
        params: list[Any] = []
        if key_id is not None:
            clauses.append("key_id = ?")
            params.append(key_id)
        if since is not None:
            clauses.append("ts >= ?")
            params.append(since)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(max(1, min(limit, 1000)))
        rows = self._connect().execute(
            f"SELECT * FROM request_log{where} ORDER BY id DESC LIMIT ?",
            params,
        ).fetchall()
        return [_log_from_row(row) for row in rows]

    def counts(self) -> dict[str, int]:
        conn = self._connect()
        active = int(
            conn.execute(
                "SELECT COUNT(*) FROM api_keys WHERE revoked_at IS NULL AND enabled = 1"
            ).fetchone()[0]
        )
        total_keys = int(conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0])
        requests = int(conn.execute("SELECT COUNT(*) FROM request_log").fetchone()[0])
        return {"active_keys": active, "total_keys": total_keys, "requests": requests}

    def prune_requests(self, before: int) -> int:
        """Delete old request-level rows; aggregate quota buckets stay intact."""

        with self._transaction() as conn:
            cursor = conn.execute("DELETE FROM request_log WHERE ts < ?", (int(before),))
            return int(cursor.rowcount)


def _clean_name(name: str) -> str:
    clean = name.strip()
    if not clean:
        raise UserError("key 名称不能为空")
    if len(clean) > 100:
        raise UserError("key 名称不能超过 100 个字符")
    if any(char in clean for char in "\r\n\x00"):
        raise UserError("key 名称不能包含换行或空字符")
    return clean


def _clean_note(note: str) -> str:
    clean = note.strip()
    if len(clean) > 500:
        raise UserError("备注不能超过 500 个字符")
    if "\x00" in clean:
        raise UserError("备注不能包含空字符")
    return clean


def _validate_key_limits(*values: int | None) -> None:
    for value in values:
        if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
            raise UserError("价格和限额必须是非负整数或 null")


def _clean_model(model: str) -> str:
    clean = model.strip()
    if not clean:
        raise UserError("模型名称不能为空")
    if len(clean) > 200:
        raise UserError("模型名称不能超过 200 个字符")
    if any(char in clean for char in "\r\n\x00"):
        raise UserError("模型名称不能包含换行或空字符")
    return clean


def _key_from_row(row: sqlite3.Row) -> ApiKey:
    return ApiKey(
        id=int(row["id"]),
        name=str(row["name"]),
        key_prefix=str(row["key_prefix"]),
        enabled=bool(row["enabled"]),
        revoked_at=int(row["revoked_at"]) if row["revoked_at"] is not None else None,
        created_at=int(row["created_at"]),
        last_used_at=int(row["last_used_at"]) if row["last_used_at"] is not None else None,
        price_in_micros_per_mtok=int(row["price_in_micros_per_mtok"]),
        price_out_micros_per_mtok=int(row["price_out_micros_per_mtok"]),
        daily_token_limit=(
            int(row["daily_token_limit"]) if row["daily_token_limit"] is not None else None
        ),
        monthly_token_limit=(
            int(row["monthly_token_limit"]) if row["monthly_token_limit"] is not None else None
        ),
        daily_cost_limit_micros=(
            int(row["daily_cost_limit_micros"]) if row["daily_cost_limit_micros"] is not None else None
        ),
        monthly_cost_limit_micros=(
            int(row["monthly_cost_limit_micros"])
            if row["monthly_cost_limit_micros"] is not None
            else None
        ),
        note=str(row["note"]),
    )


def _model_price_from_row(row: sqlite3.Row) -> ModelPrice:
    return ModelPrice(
        key_id=int(row["key_id"]) if row["key_id"] is not None else None,
        model=str(row["model"]),
        input_micros_per_mtok=int(row["input_micros_per_mtok"]),
        output_micros_per_mtok=int(row["output_micros_per_mtok"]),
        cached_input_micros_per_mtok=(
            int(row["cached_input_micros_per_mtok"])
            if row["cached_input_micros_per_mtok"] is not None
            else None
        ),
        updated_at=int(row["updated_at"]),
    )


def _totals_from_row(row: sqlite3.Row | None) -> UsageTotals:
    if row is None:
        return EMPTY_TOTALS
    return UsageTotals(
        input_tokens=int(row["input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        requests=int(row["requests"]),
        cost_micros=int(row["cost_micros"]),
        cached_input_tokens=int(row["cached_input_tokens"]),
    )


def _log_from_row(row: sqlite3.Row) -> RequestLogEntry:
    return RequestLogEntry(
        id=int(row["id"]),
        request_id=str(row["request_id"]),
        key_id=int(row["key_id"]) if row["key_id"] is not None else None,
        key_name=str(row["key_name"]),
        ts=int(row["ts"]),
        endpoint=str(row["endpoint"]),
        model=str(row["model"]) if row["model"] is not None else None,
        status=int(row["status"]),
        input_tokens=int(row["input_tokens"]),
        cached_input_tokens=int(row["cached_input_tokens"]),
        output_tokens=int(row["output_tokens"]),
        cost_micros=int(row["cost_micros"]),
        estimated=bool(row["estimated"]),
        latency_ms=int(row["latency_ms"]),
        client_ip=str(row["client_ip"]) if row["client_ip"] is not None else None,
        error=str(row["error"]) if row["error"] is not None else None,
    )
