from __future__ import annotations

import sqlite3
import threading
import time
import unittest
from pathlib import Path

from token_router.config import Config
from token_router.db import Database
from token_router.quota import QuotaExceeded, QuotaManager
from token_router.usage import TokenUsage
from token_router.util import cost_micros, hash_api_key, period_bounds
from tests._tmp import workspace_temp_dir


class DatabaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = workspace_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            data_dir=Path(self.tmp.name) / "data",
            timezone_offset_minutes=480,
            default_max_output_tokens=10,
        )
        self.db = Database(self.config.database_path, self.config.timezone_offset_minutes)
        self.db.initialize()
        self.addCleanup(self.db.close_thread_connection)

    def _create_key(self, name: str = "test", **overrides: object):
        values: dict[str, object] = {
            "name": name,
            "api_key": "tr_unit_test_key_" + name,
            "price_in_micros_per_mtok": 1_000_000,
            "price_out_micros_per_mtok": 2_000_000,
        }
        values.update(overrides)
        return self.db.create_key(**values)  # type: ignore[arg-type]

    def test_create_lookup_and_rotate(self) -> None:
        key = self._create_key()
        self.assertEqual(self.db.get_key_by_hash("bad"), None)
        found = self.db.get_key_by_hash(hash_api_key("tr_unit_test_key_test"))
        assert found is not None
        self.assertEqual(found.id, key.id)
        rotated = self.db.rotate_key(key.id, "tr_unit_test_key_rotated")
        self.assertIsNone(self.db.get_key_by_hash(hash_api_key("tr_unit_test_key_test")))
        self.assertIsNotNone(self.db.get_key_by_hash(hash_api_key("tr_unit_test_key_rotated")))
        self.assertEqual(rotated.id, key.id)

    def test_update_and_revoke(self) -> None:
        key = self._create_key()
        updated = self.db.update_key(key.id, daily_token_limit=100, name="renamed")
        self.assertEqual(updated.daily_token_limit, 100)
        self.assertEqual(updated.name, "renamed")
        revoked = self.db.revoke_key(key.id)
        self.assertIsNotNone(revoked.revoked_at)
        self.assertFalse(revoked.active)
        self.assertIsNone(self.db.get_key_by_hash(hash_api_key("tr_unit_test_key_test")))

    def test_model_price_resolution_precedence(self) -> None:
        key = self._create_key("priced")
        other = self._create_key("other")
        global_price = self.db.set_model_price(
            model="gpt-x",
            input_micros_per_mtok=3_000_000,
            output_micros_per_mtok=4_000_000,
            cached_input_micros_per_mtok=1_000_000,
        )
        self.assertEqual(global_price.scope, "global")
        key_price = self.db.set_model_price(
            model="gpt-x",
            input_micros_per_mtok=5_000_000,
            output_micros_per_mtok=6_000_000,
            key_id=key.id,
        )
        self.assertEqual(key_price.scope, "key")
        resolved = self.db.resolve_price(key, "gpt-x")
        self.assertEqual(
            (
                resolved.input_micros_per_mtok,
                resolved.output_micros_per_mtok,
                resolved.cached_input_micros_per_mtok,
                resolved.source,
            ),
            (5_000_000, 6_000_000, 5_000_000, "key-model"),
        )
        inherited = self.db.resolve_price(other, "gpt-x")
        self.assertEqual(
            (
                inherited.input_micros_per_mtok,
                inherited.output_micros_per_mtok,
                inherited.cached_input_micros_per_mtok,
                inherited.source,
            ),
            (3_000_000, 4_000_000, 1_000_000, "global-model"),
        )
        fallback = self.db.resolve_price(key, "unknown-model")
        self.assertEqual(
            (
                fallback.input_micros_per_mtok,
                fallback.output_micros_per_mtok,
                fallback.cached_input_micros_per_mtok,
                fallback.source,
            ),
            (1_000_000, 2_000_000, 1_000_000, "key-default"),
        )
        self.assertEqual(len(self.db.list_model_prices()), 1)
        self.assertEqual(len(self.db.list_model_prices(key_id=key.id, include_global=True)), 2)
        self.assertTrue(self.db.remove_model_price("gpt-x", key_id=key.id))
        self.assertEqual(self.db.resolve_price(key, "gpt-x").source, "global-model")
        self.assertTrue(self.db.remove_model_price("gpt-x"))
        self.assertEqual(self.db.resolve_price(key, "gpt-x").source, "key-default")

    def test_record_request_tracks_cached_tokens(self) -> None:
        key = self._create_key()
        now = int(time.time())
        bounds = period_bounds(now, 480)
        cost = cost_micros(100, 10, 2_000_000, 10_000_000, 60, 500_000)
        self.assertEqual(cost, 210)
        self.db.record_request(
            request_id="req_cached",
            key=key,
            timestamp=now,
            endpoint="/v1/chat/completions",
            model="cached-model",
            status=200,
            usage=TokenUsage(100, 10, 60),
            cost_micros=cost,
            latency_ms=3,
            client_ip="127.0.0.1",
        )
        day, _ = self.db.get_period_usage(key.id, bounds)
        self.assertEqual(day.cached_input_tokens, 60)
        self.assertEqual(day.uncached_input_tokens, 40)
        self.assertEqual(day.cost_micros, 210)
        log = self.db.recent_requests(key_id=key.id)[0]
        self.assertEqual(log.cached_input_tokens, 60)
        self.assertEqual(log.cost_micros, 210)

    def test_v1_database_is_migrated_to_v2(self) -> None:
        path = Path(self.tmp.name) / "legacy" / "router.db"
        path.parent.mkdir(parents=True)
        conn = sqlite3.connect(path)
        try:
            conn.executescript(
                """
                CREATE TABLE api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    key_hash TEXT NOT NULL UNIQUE,
                    key_prefix TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    revoked_at INTEGER,
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    price_in_micros_per_mtok INTEGER NOT NULL DEFAULT 0,
                    price_out_micros_per_mtok INTEGER NOT NULL DEFAULT 0,
                    daily_token_limit INTEGER,
                    monthly_token_limit INTEGER,
                    daily_cost_limit_micros INTEGER,
                    monthly_cost_limit_micros INTEGER,
                    note TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE usage_buckets (
                    key_id INTEGER NOT NULL REFERENCES api_keys(id) ON DELETE CASCADE,
                    period TEXT NOT NULL,
                    period_start INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    requests INTEGER NOT NULL DEFAULT 0,
                    cost_micros INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (key_id, period, period_start)
                ) WITHOUT ROWID;
                CREATE TABLE request_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request_id TEXT NOT NULL UNIQUE,
                    key_id INTEGER REFERENCES api_keys(id) ON DELETE SET NULL,
                    key_name TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    endpoint TEXT NOT NULL,
                    model TEXT,
                    status INTEGER NOT NULL,
                    input_tokens INTEGER NOT NULL DEFAULT 0,
                    output_tokens INTEGER NOT NULL DEFAULT 0,
                    cost_micros INTEGER NOT NULL DEFAULT 0,
                    estimated INTEGER NOT NULL DEFAULT 0,
                    latency_ms INTEGER NOT NULL DEFAULT 0,
                    client_ip TEXT,
                    error TEXT
                );
                INSERT INTO api_keys (
                    name, key_hash, key_prefix, created_at,
                    price_in_micros_per_mtok, price_out_micros_per_mtok
                ) VALUES ('legacy', 'hash', 'tr_legacy', 1, 1000000, 2000000);
                INSERT INTO usage_buckets VALUES (1, 'day', 100, 10, 5, 1, 20);
                INSERT INTO request_log (
                    request_id, key_id, key_name, ts, endpoint, model, status,
                    input_tokens, output_tokens, cost_micros
                ) VALUES ('req_legacy', 1, 'legacy', 100, '/v1/chat/completions', 'm', 200, 10, 5, 20);
                PRAGMA user_version=1;
                """
            )
        finally:
            conn.close()
        legacy = Database(path, 480)
        legacy.initialize()
        self.addCleanup(legacy.close_thread_connection)
        conn = sqlite3.connect(path)
        try:
            self.assertEqual(conn.execute("PRAGMA user_version").fetchone()[0], 2)
            for table in ("usage_buckets", "request_log"):
                columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
                self.assertIn("cached_input_tokens", columns)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM model_prices").fetchone()[0], 0)
            self.assertEqual(
                conn.execute("SELECT cached_input_tokens FROM usage_buckets").fetchone()[0], 0
            )
        finally:
            conn.close()
        day, _ = legacy.get_period_usage(1, period_bounds(100, 480))
        # The old bucket belongs to an old period, so load it directly via SQL.
        self.assertEqual(day.total_tokens, 0)
        log = legacy.recent_requests(key_id=1)[0]
        self.assertEqual(log.cached_input_tokens, 0)
        self.assertEqual(log.total_tokens, 15)
        legacy.set_model_price(
            model="legacy-model",
            input_micros_per_mtok=1_000_000,
            output_micros_per_mtok=2_000_000,
        )
        self.assertEqual(len(legacy.list_model_prices()), 1)

    def test_record_request_updates_both_buckets(self) -> None:
        key = self._create_key()
        now = int(time.time())
        bounds = period_bounds(now, 480)
        self.db.record_request(
            request_id="req_unit_1",
            key=key,
            timestamp=now,
            endpoint="/v1/chat/completions",
            model="fake",
            status=200,
            usage=TokenUsage(10, 5),
            cost_micros=cost_micros(10, 5, 1_000_000, 2_000_000),
            latency_ms=3,
            client_ip="127.0.0.1",
        )
        day, month = self.db.get_period_usage(key.id, bounds)
        self.assertEqual(day.total_tokens, 15)
        self.assertEqual(month.total_tokens, 15)
        self.assertEqual(day.requests, 1)
        self.assertEqual(day.cost_micros, 20)
        logs = self.db.recent_requests(key_id=key.id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].request_id, "req_unit_1")
        self.assertFalse(logs[0].estimated)

    def test_error_request_has_no_usage(self) -> None:
        key = self._create_key()
        now = int(time.time())
        bounds = period_bounds(now, 480)
        self.db.record_request(
            request_id="req_unit_error",
            key=key,
            timestamp=now,
            endpoint="/v1/chat/completions",
            model="fake",
            status=500,
            usage=TokenUsage(),
            cost_micros=0,
            latency_ms=2,
            client_ip="127.0.0.1",
            error="upstream status 500",
        )
        day, _ = self.db.get_period_usage(key.id, bounds)
        self.assertEqual(day.total_tokens, 0)
        self.assertEqual(day.requests, 0)
        self.assertEqual(self.db.recent_requests(key_id=key.id)[0].status, 500)

    def test_prune_requests_keeps_aggregate_buckets(self) -> None:
        key = self._create_key()
        old = int(time.time()) - 100 * 86400
        self.db.record_request(
            request_id="req_old",
            key=key,
            timestamp=old,
            endpoint="/v1/chat/completions",
            model="fake",
            status=200,
            usage=TokenUsage(10, 5),
            cost_micros=20,
            latency_ms=2,
            client_ip="127.0.0.1",
        )
        self.assertEqual(self.db.prune_requests(int(time.time()) - 90 * 86400), 1)
        self.assertEqual(self.db.recent_requests(key_id=key.id), [])
        day, _ = self.db.get_period_usage(key.id, period_bounds(old, 480))
        self.assertEqual(day.total_tokens, 15)
        self.assertEqual(day.cost_micros, 20)


class QuotaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = workspace_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            data_dir=Path(self.tmp.name) / "data",
            timezone_offset_minutes=480,
            default_max_output_tokens=10,
        )
        self.db = Database(self.config.database_path, self.config.timezone_offset_minutes)
        self.db.initialize()
        self.addCleanup(self.db.close_thread_connection)
        self.quota = QuotaManager(self.db, self.config)
        self.payload = {
            "model": "fake",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        }
        self.now = int(time.time())

    def _key(self, **overrides: object):
        values: dict[str, object] = {
            "name": "quota",
            "api_key": "tr_quota_test_key",
            "price_in_micros_per_mtok": 1_000_000,
            "price_out_micros_per_mtok": 2_000_000,
        }
        values.update(overrides)
        return self.db.create_key(**values)  # type: ignore[arg-type]

    def test_reservation_blocks_concurrent_overspend_and_releases(self) -> None:
        key = self._key(daily_token_limit=30)
        first = self.quota.reserve(key, self.payload, "/v1/chat/completions", now=self.now)
        second = self.quota.reserve(key, self.payload, "/v1/chat/completions", now=self.now)
        with self.assertRaises(QuotaExceeded) as context:
            self.quota.reserve(key, self.payload, "/v1/chat/completions", now=self.now)
        self.assertEqual(context.exception.violation.period, "day")
        self.assertEqual(context.exception.violation.kind, "tokens")
        self.quota.settle(
            first,
            request_id="req_q1",
            endpoint="/v1/chat/completions",
            model="fake",
            status=200,
            usage=TokenUsage(2, 2),
            latency_ms=1,
            client_ip="127.0.0.1",
        )
        self.quota.settle(
            second,
            request_id="req_q2",
            endpoint="/v1/chat/completions",
            model="fake",
            status=200,
            usage=TokenUsage(2, 2),
            latency_ms=1,
            client_ip="127.0.0.1",
        )
        third = self.quota.reserve(key, self.payload, "/v1/chat/completions", now=self.now)
        self.assertIsNotNone(third)

    def test_cost_limit(self) -> None:
        key = self._key(daily_cost_limit_micros=50, daily_token_limit=None)
        self.quota.reserve(key, self.payload, "/v1/chat/completions", now=self.now)
        self.quota.reserve(key, self.payload, "/v1/chat/completions", now=self.now)
        with self.assertRaises(QuotaExceeded) as context:
            self.quota.reserve(key, self.payload, "/v1/chat/completions", now=self.now)
        self.assertEqual(context.exception.violation.kind, "cost")

    def test_actual_usage_is_recorded(self) -> None:
        key = self._key(daily_token_limit=100)
        reservation = self.quota.reserve(key, self.payload, "/v1/chat/completions", now=self.now)
        result = self.quota.settle(
            reservation,
            request_id="req_q3",
            endpoint="/v1/chat/completions",
            model="fake",
            status=200,
            usage=TokenUsage(7, 3),
            latency_ms=2,
            client_ip="127.0.0.1",
        )
        self.assertEqual(result.usage.total_tokens, 10)
        self.assertEqual(result.cost_micros, 13)
        bounds = period_bounds(self.now, 480)
        day, _ = self.db.get_period_usage(key.id, bounds)
        self.assertEqual(day.total_tokens, 10)
        self.assertEqual(self.quota.reserved_for(key.id), (0, 0))

    def test_unmetered_models_request_does_not_consume_quota(self) -> None:
        key = self._key(daily_token_limit=1)
        reservation = self.quota.reserve(key, None, "/v1/models", now=self.now)
        self.quota.settle(
            reservation,
            request_id="req_q4",
            endpoint="/v1/models",
            model=None,
            status=200,
            usage=TokenUsage(),
            latency_ms=1,
            client_ip="127.0.0.1",
        )
        day, _ = self.db.get_period_usage(key.id, period_bounds(self.now, 480))
        self.assertEqual(day.requests, 0)

    def test_concurrent_reservations_are_serialized(self) -> None:
        key = self._key(daily_token_limit=50)
        barrier = threading.Barrier(10)
        successes: list[object] = []
        failures: list[int] = []
        lock = threading.Lock()

        def worker(index: int) -> None:
            try:
                barrier.wait(timeout=5)
                try:
                    reservation = self.quota.reserve(
                        key, self.payload, "/v1/chat/completions", now=self.now
                    )
                    with lock:
                        successes.append(reservation)
                except QuotaExceeded:
                    with lock:
                        failures.append(index)
            finally:
                self.db.close_thread_connection()

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertTrue(successes)
        self.assertTrue(failures)
        reserved, _ = self.quota.reserved_for(key.id)
        self.assertLessEqual(reserved, 50)
        for index, reservation in enumerate(successes):
            self.quota.settle(
                reservation,  # type: ignore[arg-type]
                request_id=f"req_concurrent_{index}",
                endpoint="/v1/chat/completions",
                model="fake",
                status=200,
                usage=TokenUsage(),
                latency_ms=1,
                client_ip="127.0.0.1",
            )
        self.assertEqual(self.quota.reserved_for(key.id), (0, 0))


if __name__ == "__main__":
    unittest.main()
