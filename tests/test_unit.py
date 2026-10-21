from __future__ import annotations

import json
import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_router.config import Config, ConfigError, load_config, write_default_config
from token_router.upstream import UpstreamClient
from token_router.usage import (
    SSEUsageExtractor,
    estimate_prompt_tokens,
    extract_usage,
    inject_stream_usage,
    requested_output_tokens,
)
from token_router.util import (
    UserError,
    cost_micros,
    estimate_text_tokens,
    format_amount,
    parse_money_to_micros,
    parse_optional_money,
    parse_token_quantity,
    period_bounds,
)
from tests._tmp import workspace_temp_dir


class UtilTests(unittest.TestCase):
    def test_parse_token_quantity(self) -> None:
        self.assertEqual(parse_token_quantity("100000"), 100000)
        self.assertEqual(parse_token_quantity("100k"), 100000)
        self.assertEqual(parse_token_quantity("1.5m"), 1_500_000)
        self.assertEqual(parse_token_quantity("2B"), 2_000_000_000)
        self.assertEqual(parse_token_quantity("1.2k"), 1_200)
        self.assertIsNone(parse_token_quantity("none"))
        self.assertIsNone(parse_token_quantity("unlimited"))
        with self.assertRaises(UserError):
            parse_token_quantity("0.0001k")
        with self.assertRaises(UserError):
            parse_token_quantity("-1")

    def test_money_and_cost(self) -> None:
        self.assertEqual(parse_money_to_micros("2.50"), 2_500_000)
        self.assertEqual(parse_money_to_micros("0.000001"), 1)
        self.assertIsNone(parse_optional_money("none"))
        self.assertEqual(parse_optional_money("2.5"), 2_500_000)
        self.assertEqual(format_amount(2_500_000, "$"), "$2.5")
        self.assertEqual(format_amount(1, "$"), "$0.000001")
        self.assertEqual(cost_micros(1_000_000, 0, 1_000_000, 0), 1_000_000)
        self.assertEqual(cost_micros(10, 5, 1_000_000, 2_000_000), 20)
        self.assertEqual(cost_micros(0, 0, 1_000_000, 2_000_000), 0)
        self.assertEqual(
            cost_micros(100, 10, 1_000_000, 2_000_000, 60, 500_000),
            90,
        )
        self.assertEqual(cost_micros(100, 10, 1_000_000, 2_000_000, 60), 120)
        self.assertEqual(cost_micros(10, 0, 1_000_000, 0, 99, 0), 0)

    def test_period_bounds_respect_fixed_offset(self) -> None:
        tz = timezone(timedelta(hours=8))
        moment = datetime(2026, 10, 21, 23, 30, tzinfo=tz)
        bounds = period_bounds(moment.timestamp(), 480)
        self.assertEqual(
            datetime.fromtimestamp(bounds.day_start, tz).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-10-21 00:00:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(bounds.next_day, tz).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-10-22 00:00:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(bounds.month_start, tz).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-10-01 00:00:00",
        )
        self.assertEqual(
            datetime.fromtimestamp(bounds.next_month, tz).strftime("%Y-%m-%d %H:%M:%S"),
            "2026-11-01 00:00:00",
        )

    def test_text_estimate_cjk_and_ascii(self) -> None:
        self.assertGreater(estimate_text_tokens("你好世界"), estimate_text_tokens("hello"))
        self.assertEqual(estimate_text_tokens(""), 0)


class UsageTests(unittest.TestCase):
    def test_extract_chat_usage(self) -> None:
        usage = extract_usage(
            {"usage": {"prompt_tokens": 11, "completion_tokens": 4, "total_tokens": 15}}
        )
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.total_tokens), (11, 4, 15))
        self.assertFalse(usage.estimated)

    def test_extract_responses_usage_nested(self) -> None:
        usage = extract_usage(
            {"type": "response.completed", "response": {"usage": {"input_tokens": 12, "output_tokens": 8}}}
        )
        self.assertIsNotNone(usage)
        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens), (12, 8))

    def test_extract_total_only(self) -> None:
        usage = extract_usage({"usage": {"total_tokens": 42}})
        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens), (42, 0))

    def test_extract_cached_tokens(self) -> None:
        usage = extract_usage(
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_tokens_details": {"cached_tokens": 60},
                }
            }
        )
        assert usage is not None
        self.assertEqual((usage.input_tokens, usage.output_tokens, usage.cached_input_tokens), (100, 10, 60))
        self.assertEqual(usage.uncached_input_tokens, 40)

        deepseek = extract_usage(
            {
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 10,
                    "prompt_cache_hit_tokens": 70,
                    "prompt_cache_miss_tokens": 30,
                }
            }
        )
        assert deepseek is not None
        self.assertEqual(deepseek.cached_input_tokens, 70)

        clamped = extract_usage(
            {"usage": {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 99}}}
        )
        assert clamped is not None
        self.assertEqual(clamped.cached_input_tokens, 5)

    def test_inject_stream_usage(self) -> None:
        body = json.dumps(
            {
                "model": "fake",
                "stream": True,
                "stream_options": {"foo": "bar"},
                "messages": [{"role": "user", "content": "hi"}],
            }
        ).encode()
        injected = json.loads(inject_stream_usage(body, "/v1/chat/completions", enabled=True))
        self.assertTrue(injected["stream_options"]["include_usage"])
        self.assertEqual(injected["stream_options"]["foo"], "bar")
        unchanged = inject_stream_usage(body, "/v1/embeddings", enabled=True)
        self.assertEqual(unchanged, body)
        disabled = inject_stream_usage(body, "/v1/chat/completions", enabled=False)
        self.assertEqual(disabled, body)

    def test_prompt_and_output_reservation(self) -> None:
        payload = {
            "model": "fake",
            "messages": [{"role": "user", "content": "你好，请写一段测试文本"}],
            "max_tokens": 123,
        }
        self.assertGreater(estimate_prompt_tokens(payload), 5)
        self.assertEqual(requested_output_tokens(payload, "/v1/chat/completions", 4096), 123)
        self.assertEqual(
            requested_output_tokens({"input": "x"}, "/v1/embeddings", 4096),
            0,
        )

    def test_sse_extractor_handles_split_chunks(self) -> None:
        stream = (
            b'data: {"choices":[{"delta":{"content":"hello "}}]}\n\n'
            b'data: {"choices":[{"delta":{"content":"world"}}]}\n\n'
            b'data: {"choices":[],"usage":{"prompt_tokens":11,"completion_tokens":4,'
            b'"prompt_tokens_details":{"cached_tokens":6}}}\n\n'
            b"data: [DONE]\n\n"
        )
        extractor = SSEUsageExtractor()
        for index in range(0, len(stream), 7):
            extractor.feed(stream[index : index + 7])
        extractor.finish()
        self.assertEqual(extractor.event_count, 3)
        assert extractor.usage is not None
        self.assertEqual(
            (extractor.usage.input_tokens, extractor.usage.output_tokens, extractor.usage.cached_input_tokens),
            (11, 4, 6),
        )
        self.assertGreater(extractor.estimated_output_tokens, 0)

    def test_sse_extractor_responses_event(self) -> None:
        stream = (
            b'data: {"type":"response.output_text.delta","delta":"abc"}\n\n'
            b'data: {"type":"response.completed","response":{"usage":{"input_tokens":9,"output_tokens":2}}}\n\n'
        )
        extractor = SSEUsageExtractor()
        extractor.feed(stream)
        extractor.finish()
        assert extractor.usage is not None
        self.assertEqual(extractor.usage.input_tokens, 9)


class ConfigAndUpstreamTests(unittest.TestCase):
    def test_config_relative_data_dir_and_env_override(self) -> None:
        with workspace_temp_dir() as tmp:
            path = Path(tmp) / "config.json"
            write_default_config(path)
            document = json.loads(path.read_text(encoding="utf-8"))
            document["data_dir"] = "runtime"
            path.write_text(json.dumps(document), encoding="utf-8")
            old = os.environ.get("TR_PORT")
            os.environ["TR_PORT"] = "9999"
            try:
                config = load_config(path, require_exists=True)
            finally:
                if old is None:
                    os.environ.pop("TR_PORT", None)
                else:
                    os.environ["TR_PORT"] = old
            self.assertEqual(config.port, 9999)
            self.assertEqual(config.data_dir, (Path(tmp) / "runtime").resolve())

    def test_config_rejects_unknown_fields(self) -> None:
        with workspace_temp_dir() as tmp:
            path = Path(tmp) / "config.json"
            path.write_text('{"nope": 1}', encoding="utf-8")
            with self.assertRaises(ConfigError):
                load_config(path, require_exists=True)

    def test_map_target(self) -> None:
        base = Config(upstream_base_url="https://example.com/v1")
        client = UpstreamClient(base, "secret")
        self.assertEqual(client.map_target("/v1/chat/completions"), "/v1/chat/completions")
        self.assertEqual(client.map_target("/v1/models", "limit=1"), "/v1/models?limit=1")
        base2 = Config(upstream_base_url="https://example.com")
        client2 = UpstreamClient(base2, "secret")
        self.assertEqual(client2.map_target("/v1/chat/completions"), "/v1/chat/completions")
        base3 = Config(upstream_base_url="https://example.com/openai/v1")
        client3 = UpstreamClient(base3, "secret")
        self.assertEqual(client3.map_target("/v1/responses"), "/openai/v1/responses")


if __name__ == "__main__":
    unittest.main()
