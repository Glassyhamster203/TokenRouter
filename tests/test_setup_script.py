from __future__ import annotations

import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tests._tmp import workspace_temp_dir

ROOT = Path(__file__).resolve().parent.parent


class SetupScriptTests(unittest.TestCase):
    def test_embedded_config_patch_generates_and_preserves_keys(self) -> None:
        script = (ROOT / "setup-linux-cny.sh").read_text(encoding="utf-8")
        match = re.search(r"<<'PY'\n(.*?)\nPY", script, re.S)
        assert match is not None
        with workspace_temp_dir() as tmp:
            working = Path(tmp)
            config = working / "config.json"
            config.write_text((ROOT / "config.json").read_text(encoding="utf-8"), encoding="utf-8")
            patch = working / "patch.py"
            patch.write_text(match.group(1), encoding="utf-8")
            base_args = [
                sys.executable,
                str(patch),
                str(config),
                "https://www.example.com/v1",
                "127.0.0.1",
                "8787",
                "0",
                "user-a:60",
                "user-b:60",
                "user-c:20",
            ]
            first = subprocess.run(base_args, check=True, text=True, capture_output=True)
            self.assertEqual(len(first.stdout.strip().splitlines()), 3)
            document = json.loads(config.read_text(encoding="utf-8"))
            self.assertEqual(document["currency"], "CNY")
            self.assertEqual(document["currency_symbol"], "¥")
            self.assertEqual([user["daily_cost_limit"] for user in document["users"]], [60.0, 60.0, 20.0])
            self.assertTrue(all(user["allowed_models"] == ["*"] for user in document["users"]))
            old_keys = [user["api_key"] for user in document["users"]]

            second = subprocess.run(base_args, check=True, text=True, capture_output=True)
            self.assertEqual(second.stdout.strip(), "")
            self.assertEqual(
                [user["api_key"] for user in json.loads(config.read_text(encoding="utf-8"))["users"]],
                old_keys,
            )

            rotated_args = list(base_args)
            rotated_args[6] = "1"
            third = subprocess.run(rotated_args, check=True, text=True, capture_output=True)
            self.assertEqual(len(third.stdout.strip().splitlines()), 3)
            new_keys = [user["api_key"] for user in json.loads(config.read_text(encoding="utf-8"))["users"]]
            self.assertNotEqual(new_keys, old_keys)


if __name__ == "__main__":
    unittest.main()
