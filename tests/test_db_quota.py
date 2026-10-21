from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
