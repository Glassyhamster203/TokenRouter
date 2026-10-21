from __future__ import annotations

import http.client
import json
import os
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from token_router.config import Config
from token_router.db import Database
from token_router.proxy import build_application, make_server
from token_router.util import cost_micros, generate_api_key, period_bounds
from tests._tmp import workspace_temp_dir


class FakeUpstreamState:
    lock = threading.Lock()
    requests: list[dict[str, object]] = []

    @classmethod
    def reset(cls) -> None:
        with cls.lock:
            cls.requests = []

    @classmethod
    def record(cls, item: dict[str, object]) -> None:
        with cls.lock:
            cls.requests.append(item)

    @classmethod
    def snapshot(cls) -> list[dict[str, object]]:
        with cls.lock:
            return list(cls.requests)


class FakeUpstreamHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "FakeUpstream/1.0"

    def log_message(self, format: str, *args: object) -> None:
        return

    def _read_body(self) -> bytes:
        length = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(length) if length else b""

    def _send_json(self, status: int, payload: dict[str, object]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()

    def do_GET(self) -> None:
        FakeUpstreamState.record(
            {
                "method": "GET",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": None,
                "client_port": self.client_address[1],
            }
        )
        if self.path == "/v1/models":
            self._send_json(200, {"object": "list", "data": [{"id": "fake-model", "object": "model"}]})
        else:
            self._send_json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        raw = self._read_body()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            payload = {}
        FakeUpstreamState.record(
            {
                "method": "POST",
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": payload,
                "client_port": self.client_address[1],
            }
        )
        if self.path != "/v1/chat/completions":
            self._send_json(404, {"error": {"message": "not found"}})
            return
        model = payload.get("model")
        if model == "fail":
            self._send_json(500, {"error": {"message": "upstream exploded"}})
            return
        if model == "big":
            prompt_tokens, completion_tokens = 80, 10
        elif model == "cached":
            prompt_tokens, completion_tokens = 100, 10
        else:
            prompt_tokens, completion_tokens = 10, 5

        if payload.get("stream") is True:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            events = [
                b'data: {"id":"fake","choices":[{"delta":{"content":"hello "}}]}\n\n',
                b'data: {"id":"fake","choices":[{"delta":{"content":"world"}}]}\n\n',
            ]
            if model != "stream-no-usage":
                usage = {
                    "prompt_tokens": 7,
                    "completion_tokens": 3,
                    "total_tokens": 10,
                }
                events.append(
                    (
                        "data: "
                        + json.dumps({"id": "fake", "choices": [], "usage": usage})
                        + "\n\n"
                    ).encode()
                )
            events.append(b"data: [DONE]\n\n")
            for event in events:
                self.wfile.write(f"{len(event):X}\r\n".encode())
                self.wfile.write(event)
                self.wfile.write(b"\r\n")
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
            return

        response: dict[str, object] = {
            "id": "fake",
            "object": "chat.completion",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "hello world"},
                    "finish_reason": "stop",
                }
            ],
        }
        if model != "no-usage":
            response["usage"] = {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            }
            if model == "cached":
                response["usage"]["prompt_tokens_details"] = {"cached_tokens": 60}
        self._send_json(200, response)


class ProxyIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = workspace_temp_dir()
        os.environ["TR_UPSTREAM_API_KEY"] = "sk-upstream-secret"
        cls.fake_server = ThreadingHTTPServer(("127.0.0.1", 0), FakeUpstreamHandler)
        cls.fake_server.daemon_threads = True
        cls.fake_thread = threading.Thread(target=cls.fake_server.serve_forever, daemon=True)
        cls.fake_thread.start()
        fake_port = cls.fake_server.server_address[1]

        cls.config = Config(
            host="127.0.0.1",
            port=0,
            upstream_base_url=f"http://127.0.0.1:{fake_port}/v1",
            data_dir=Path(cls.tmp.name) / "data",
            timezone_offset_minutes=480,
            currency="USD",
            currency_symbol="$",
            default_price_in_micros_per_mtok=1_000_000,
            default_price_out_micros_per_mtok=2_000_000,
            default_max_output_tokens=16,
            max_request_bytes=1024 * 1024,
            max_response_bytes=4 * 1024 * 1024,
            max_concurrency=16,
            connect_timeout_seconds=5.0,
            request_timeout_seconds=10.0,
            upstream_pool_size=4,
            upstream_pool_idle_seconds=30.0,
            inject_stream_usage=True,
        )
        cls.app = build_application(cls.config)
        cls.server = make_server(cls.app)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.db: Database = cls.app.db

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()
        cls.fake_server.shutdown()
        cls.fake_server.server_close()
        cls.db.close_thread_connection()
        os.environ.pop("TR_UPSTREAM_API_KEY", None)
        time.sleep(0.2)
        cls.tmp.cleanup()

    def setUp(self) -> None:
        FakeUpstreamState.reset()

    def _new_key(self, name: str, **overrides: object) -> tuple[object, str]:
        raw = generate_api_key()
        values: dict[str, object] = {
            "name": name,
            "api_key": raw,
            "price_in_micros_per_mtok": 1_000_000,
            "price_out_micros_per_mtok": 2_000_000,
            "daily_token_limit": None,
            "monthly_token_limit": None,
            "daily_cost_limit_micros": None,
            "monthly_cost_limit_micros": None,
        }
        values.update(overrides)
        key = self.db.create_key(**values)  # type: ignore[arg-type]
        return key, raw

    def _request(
        self,
        method: str,
        path: str,
        key: str | None,
        payload: dict[str, object] | None = None,
        *,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers: dict[str, str] = {}
        if key is not None:
            headers["Authorization"] = f"Bearer {key}"
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        data = response.read()
        response_headers = {name.lower(): value for name, value in response.getheaders()}
        connection.close()
        return response.status, response_headers, data

    def test_invalid_key_returns_401(self) -> None:
        status, headers, body = self._request(
            "POST",
            "/v1/chat/completions",
            "tr_invalid",
            {"model": "fake", "messages": [{"role": "user", "content": "hi"}]},
        )
        self.assertEqual(status, 401)
        self.assertIn("www-authenticate", headers)
        self.assertEqual(json.loads(body)["error"]["code"], "invalid_api_key")
        self.assertEqual(FakeUpstreamState.snapshot(), [])

    def test_nonstream_billing_and_upstream_key_injection(self) -> None:
        key, raw = self._new_key("billing")
        status, headers, body = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {
                "model": "fake",
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn("x-token-router-request-id", headers)
        self.assertEqual(json.loads(body)["usage"]["total_tokens"], 15)
        requests = FakeUpstreamState.snapshot()
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["authorization"], "Bearer sk-upstream-secret")
        bounds = period_bounds(int(time.time()), 480)
        day, month = self.db.get_period_usage(key.id, bounds)
        self.assertEqual((day.input_tokens, day.output_tokens), (10, 5))
        self.assertEqual((month.input_tokens, month.output_tokens), (10, 5))
        self.assertEqual(day.cost_micros, cost_micros(10, 5, 1_000_000, 2_000_000))
        logs = self.db.recent_requests(key_id=key.id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0].status, 200)
        self.assertFalse(logs[0].estimated)

    def test_two_downstream_keys_share_one_upstream_key(self) -> None:
        key_a, raw_a = self._new_key("tenant-a")
        key_b, raw_b = self._new_key("tenant-b")
        for raw in (raw_a, raw_b):
            status, _, _ = self._request(
                "POST",
                "/v1/chat/completions",
                raw,
                {"model": "fake", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
            )
            self.assertEqual(status, 200)
        requests = FakeUpstreamState.snapshot()
        self.assertEqual(len(requests), 2)
        self.assertEqual({item["authorization"] for item in requests}, {"Bearer sk-upstream-secret"})
        bounds = period_bounds(int(time.time()), 480)
        day_a, _ = self.db.get_period_usage(key_a.id, bounds)
        day_b, _ = self.db.get_period_usage(key_b.id, bounds)
        self.assertEqual(day_a.total_tokens, 15)
        self.assertEqual(day_b.total_tokens, 15)

    def test_upstream_keepalive_pool_reuses_connections(self) -> None:
        _, raw = self._new_key("keepalive")
        for _ in range(3):
            status, _, _ = self._request(
                "POST",
                "/v1/chat/completions",
                raw,
                {"model": "fake", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
            )
            self.assertEqual(status, 200)
        ports = {item["client_port"] for item in FakeUpstreamState.snapshot()}
        self.assertEqual(len(ports), 1, f"expected one pooled upstream connection, got {ports}")

    def test_daily_token_quota_is_enforced_before_forwarding(self) -> None:
        key, raw = self._new_key("quota", daily_token_limit=100)
        payload = {
            "model": "big",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        }
        status, _, _ = self._request("POST", "/v1/chat/completions", raw, payload)
        self.assertEqual(status, 200)
        status, headers, body = self._request("POST", "/v1/chat/completions", raw, payload)
        self.assertEqual(status, 429)
        self.assertEqual(json.loads(body)["error"]["code"], "quota_exceeded")
        self.assertIn("retry-after", headers)
        self.assertEqual(len(FakeUpstreamState.snapshot()), 1)
        bounds = period_bounds(int(time.time()), 480)
        day, _ = self.db.get_period_usage(key.id, bounds)
        self.assertEqual(day.total_tokens, 90)

    def test_daily_cost_quota_is_enforced(self) -> None:
        key, raw = self._new_key(
            "cost-quota",
            daily_cost_limit_micros=50,
            price_in_micros_per_mtok=1_000_000,
            price_out_micros_per_mtok=2_000_000,
        )
        payload = {
            "model": "fake",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 10,
        }
        statuses = [self._request("POST", "/v1/chat/completions", raw, payload)[0] for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])
        bounds = period_bounds(int(time.time()), 480)
        day, _ = self.db.get_period_usage(key.id, bounds)
        self.assertEqual(day.cost_micros, 40)

    def test_streaming_usage_is_injected_and_billed(self) -> None:
        key, raw = self._new_key("stream")
        status, headers, body = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {
                "model": "fake",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(headers["content-type"].startswith("text/event-stream"))
        self.assertIn(b"data: [DONE]", body)
        self.assertIn(b'"completion_tokens": 3', body)
        requests = FakeUpstreamState.snapshot()
        self.assertEqual(len(requests), 1)
        self.assertTrue(requests[0]["body"]["stream_options"]["include_usage"])
        bounds = period_bounds(int(time.time()), 480)
        day, _ = self.db.get_period_usage(key.id, bounds)
        self.assertEqual((day.input_tokens, day.output_tokens), (7, 3))
        self.assertEqual(day.cost_micros, 13)
        logs = self.db.recent_requests(key_id=key.id)
        self.assertFalse(logs[0].estimated)

    def test_stream_without_usage_falls_back_to_estimate(self) -> None:
        key, raw = self._new_key("stream-estimate")
        status, _, body = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {
                "model": "stream-no-usage",
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
                "max_tokens": 10,
            },
        )
        self.assertEqual(status, 200)
        self.assertIn(b"hello ", body)
        logs = self.db.recent_requests(key_id=key.id)
        self.assertTrue(logs[0].estimated)
        self.assertGreater(logs[0].total_tokens, 0)

    def test_missing_usage_falls_back_to_estimate(self) -> None:
        key, raw = self._new_key("no-usage")
        status, _, _ = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {"model": "no-usage", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
        )
        self.assertEqual(status, 200)
        logs = self.db.recent_requests(key_id=key.id)
        self.assertEqual(len(logs), 1)
        self.assertTrue(logs[0].estimated)
        self.assertGreater(logs[0].total_tokens, 0)

    def test_upstream_error_is_forwarded_without_charge(self) -> None:
        key, raw = self._new_key("error")
        status, _, body = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {"model": "fail", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
        )
        self.assertEqual(status, 500)
        self.assertIn(b"upstream exploded", body)
        logs = self.db.recent_requests(key_id=key.id)
        self.assertEqual(logs[0].status, 500)
        self.assertEqual(logs[0].total_tokens, 0)
        bounds = period_bounds(int(time.time()), 480)
        day, _ = self.db.get_period_usage(key.id, bounds)
        self.assertEqual(day.requests, 0)

    def test_key_model_permissions_are_enforced(self) -> None:
        key, raw = self._new_key("permissions")
        self.db.update_key(key.id, allowed_models=["fake"])
        status, _, body = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {"model": "cached", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
        )
        self.assertEqual(status, 403)
        self.assertEqual(json.loads(body)["error"]["code"], "model_not_allowed")
        self.assertEqual(FakeUpstreamState.snapshot(), [])

        status, _, _ = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {"model": "fake", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
        )
        self.assertEqual(status, 200)

        self.db.update_key(key.id, allowed_models=["gpt-4o*"])
        status, _, _ = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
        )
        self.assertEqual(status, 200)

        self.db.update_key(key.id, allowed_models=[])
        status, _, _ = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {"model": "fake", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
        )
        self.assertEqual(status, 403)

    def test_models_endpoint_is_proxied_and_not_metered(self) -> None:
        key, raw = self._new_key("models")
        status, _, body = self._request("GET", "/v1/models", raw)
        self.assertEqual(status, 200)
        self.assertIn(b"fake-model", body)
        requests = FakeUpstreamState.snapshot()
        self.assertEqual(requests[0]["authorization"], "Bearer sk-upstream-secret")
        logs = self.db.recent_requests(key_id=key.id)
        self.assertEqual(logs[0].status, 200)
        self.assertEqual(logs[0].total_tokens, 0)

    def test_model_price_and_cached_tokens_are_billed(self) -> None:
        key, raw = self._new_key("model-price")
        self.db.set_model_price(
            model="cached",
            input_micros_per_mtok=2_000_000,
            output_micros_per_mtok=10_000_000,
            cached_input_micros_per_mtok=500_000,
        )
        status, _, body = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {"model": "cached", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["usage"]["prompt_tokens_details"]["cached_tokens"], 60)
        bounds = period_bounds(int(time.time()), 480)
        day, _ = self.db.get_period_usage(key.id, bounds)
        self.assertEqual(day.input_tokens, 100)
        self.assertEqual(day.cached_input_tokens, 60)
        self.assertEqual(day.output_tokens, 10)
        # 40 uncached * 2 + 60 cached * 0.5 + 10 output * 10 = 210 micros
        self.assertEqual(day.cost_micros, 210)
        logs = self.db.recent_requests(key_id=key.id)
        self.assertEqual(logs[0].cached_input_tokens, 60)
        self.assertEqual(logs[0].cost_micros, 210)

    def test_key_model_price_overrides_global(self) -> None:
        key, raw = self._new_key("override")
        self.db.set_model_price(
            model="cached",
            input_micros_per_mtok=2_000_000,
            output_micros_per_mtok=10_000_000,
            cached_input_micros_per_mtok=500_000,
        )
        self.db.set_model_price(
            model="cached",
            input_micros_per_mtok=4_000_000,
            output_micros_per_mtok=20_000_000,
            cached_input_micros_per_mtok=1_000_000,
            key_id=key.id,
        )
        status, _, _ = self._request(
            "POST",
            "/v1/chat/completions",
            raw,
            {"model": "cached", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
        )
        self.assertEqual(status, 200)
        day, _ = self.db.get_period_usage(key.id, period_bounds(int(time.time()), 480))
        # 40 * 4 + 60 * 1 + 10 * 20 = 420 micros
        self.assertEqual(day.cost_micros, 420)

    def test_unknown_endpoint_is_rejected(self) -> None:
        key, raw = self._new_key("unknown")
        status, _, body = self._request("POST", "/v1/unknown", raw, {"model": "fake"})
        self.assertEqual(status, 404)
        self.assertEqual(json.loads(body)["error"]["code"], "not_found")
        self.assertEqual(FakeUpstreamState.snapshot(), [])

    def test_health_does_not_need_auth(self) -> None:
        status, _, body = self._request("GET", "/health", None)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "ok")

    def test_chunked_request_body_is_supported(self) -> None:
        key, raw = self._new_key("chunked")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(
            {"model": "fake", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10}
        ).encode()
        connection.request(
            "POST",
            "/v1/chat/completions",
            body=[payload[:10], payload[10:]],
            headers={"Authorization": f"Bearer {raw}", "Content-Type": "application/json"},
            encode_chunked=True,
        )
        response = connection.getresponse()
        body = response.read()
        connection.close()
        self.assertEqual(response.status, 200)
        self.assertIn(b"hello world", body)


if __name__ == "__main__":
    unittest.main()
