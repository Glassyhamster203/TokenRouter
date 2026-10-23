from __future__ import annotations

import unittest
from pathlib import Path

from token_router.pricing import parse_pricing_file, parse_pricing_markdown
from token_router.util import UserError


class PricingMarkdownTests(unittest.TestCase):
    def test_parse_example_file(self) -> None:
        path = Path(__file__).resolve().parent.parent / "模型定价.md"
        rows = {row.model: row for row in parse_pricing_file(path)}
        self.assertEqual(rows["deepseek-flash"].input_per_mtok, "2")
        self.assertEqual(rows["deepseek-flash"].cached_per_mtok, "0.2")
        self.assertEqual(rows["deepseek-flash"].output_per_mtok, "2")
        self.assertEqual(rows["glm-5.3-flash"].input_per_mtok, "4")
        self.assertEqual(rows["mineru"].cached_per_mtok, "1")
        self.assertEqual(rows["smart/default"].input_per_mtok, "0")
        self.assertEqual(rows["smart/default"].output_per_mtok, "0")
        self.assertEqual(rows["unlimited-ocr"].input_per_mtok, "1")

    def test_parse_inline_table(self) -> None:
        rows = parse_pricing_markdown(
            """
| 模型名称 | 上下文 | 输入 | 输出 |
| :--- | :--- | :--- | :--- |
| **m1** | 1000 | ¥2 / ¥0.2（每百万） | ¥3（每百万） |
| `m2` | 1000 | ¥1 / ¥0.1 | 未配置 |
"""
        )
        self.assertEqual([row.model for row in rows], ["m1", "m2"])
        self.assertEqual(rows[0].cached_per_mtok, "0.2")
        self.assertEqual(rows[0].output_per_mtok, "3")
        self.assertEqual(rows[1].output_per_mtok, "0")

    def test_empty_table_is_rejected(self) -> None:
        with self.assertRaises(UserError):
            parse_pricing_markdown("| a | b | c | d |\n| - | - | - | - |")


if __name__ == "__main__":
    unittest.main()
