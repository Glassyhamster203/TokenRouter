from __future__ import annotations

import os
import unittest
from pathlib import Path

from token_router.config import Config, ConfigUser
from token_router.config_users import sync_config_users
from token_router.db import Database
from token_router.proxy import build_application
from token_router.util import hash_api_key
from tests._tmp import workspace_temp_dir


class ConfigUserSyncTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = workspace_temp_dir()
        self.addCleanup(self.tmp.cleanup)
        self.config = Config(
            data_dir=Path(self.tmp.name) / "data",
            default_price_in_micros_per_mtok=1_000_000,
            default_price_out_micros_per_mtok=2_000_000,
            users=(
                ConfigUser(
                    name="alice",
                    api_key="tr_alice_file_key_1234567890",
                    daily_token_limit=1000,
                    monthly_cost_limit_micros=5_000_000,
                    allowed_models=("gpt-4o*",),
                    note="from file",
                ),
                ConfigUser(
                    name="bob",
                    api_key="tr_bob_file_key_1234567890",
                    allowed_models=(),
                    enabled=False,
                ),
            ),
        )
        self.db = Database(self.config.database_path, self.config.timezone_offset_minutes)
        self.db.initialize()
        self.addCleanup(self.db.close_thread_connection)

    def test_creates_and_updates_users_by_name(self) -> None:
        first = sync_config_users(self.config, self.db)
        self.assertEqual((first.created, first.updated), (2, 0))
        alice = self.db.get_key_by_name("alice")
        assert alice is not None
        self.assertEqual(alice.daily_token_limit, 1000)
        self.assertEqual(alice.allowed_models, ("gpt-4o*",))
        self.assertEqual(alice.note, "from file")
        bob = self.db.get_key_by_name("bob")
        assert bob is not None
        self.assertFalse(bob.enabled)
        self.assertEqual(bob.allowed_models, ())
        self.assertIsNotNone(self.db.get_key_by_hash(hash_api_key("tr_alice_file_key_1234567890")))

        updated_config = Config(
            data_dir=self.config.data_dir,
            default_price_in_micros_per_mtok=1_000_000,
            default_price_out_micros_per_mtok=2_000_000,
            users=(
                ConfigUser(
                    name="alice",
                    api_key="tr_alice_rotated_key_1234567890",
                    daily_token_limit=2000,
                    allowed_models=None,
                    enabled=True,
                ),
            ),
        )
        second = sync_config_users(updated_config, self.db)
        self.assertEqual((second.created, second.updated), (0, 1))
        alice = self.db.get_key_by_name("alice")
        assert alice is not None
        self.assertEqual(alice.id, 1)
        self.assertEqual(alice.daily_token_limit, 2000)
        self.assertIsNone(alice.allowed_models)
        self.assertIsNone(self.db.get_key_by_hash(hash_api_key("tr_alice_file_key_1234567890")))
        self.assertIsNotNone(self.db.get_key_by_hash(hash_api_key("tr_alice_rotated_key_1234567890")))
        self.assertIsNotNone(self.db.get_key_by_name("bob"))

    def test_revoked_user_is_not_resurrected(self) -> None:
        sync_config_users(self.config, self.db)
        alice = self.db.get_key_by_name("alice")
        assert alice is not None
        self.db.revoke_key(alice.id)
        summary = sync_config_users(self.config, self.db)
        self.assertEqual(summary.skipped_revoked, 1)
        alice = self.db.get_key_by_name("alice")
        assert alice is not None
        self.assertIsNotNone(alice.revoked_at)

    def test_build_application_syncs_file_users(self) -> None:
        old = {name: os.environ.pop(name, None) for name in ("TR_UPSTREAM_API_KEY", "TOKEN_ROUTER_UPSTREAM_KEY")}
        app = None
        try:
            config = self.config.with_overrides(upstream_api_key_value="sk-test-upstream")
            app = build_application(config)
            alice = self.db.get_key_by_name("alice")
            assert alice is not None
            self.assertEqual(alice.allowed_models, ("gpt-4o*",))
            self.assertIsNotNone(
                self.db.get_key_by_hash(hash_api_key("tr_alice_file_key_1234567890"))
            )
        finally:
            if app is not None:
                app.close()
            for name, value in old.items():
                if value is not None:
                    os.environ[name] = value


if __name__ == "__main__":
    unittest.main()
