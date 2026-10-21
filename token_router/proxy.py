"""Single-port OpenAI-compatible gateway with virtual-key metering."""

from __future__ import annotations

import http.client
import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

from . import __version__
from .config import Config
from .db import ApiKey, Database
from .quota import QuotaExceeded, QuotaManager, Reservation
from .upstream import (
    ResponseTooLarge,
    UpstreamClient,
    UpstreamError,
    UpstreamExchange,
)
from .usage import (
    EMPTY_USAGE,
    TokenUsage,
    canonical_path,
    extract_usage,
    fallback_stream_usage,
    fallback_usage,
    inject_stream_usage,
    is_billable_path,
    is_models_path,
    parse_json_object,
    stream_requested,
)
from .util import (
    format_amount,
    hash_api_key,
    new_request_id,
    retry_after_seconds,
)

logger = logging.getLogger("token_router")

_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
    "host",
    "date",
    "server",
}
_CLIENT_AUTH_HEADERS = {"authorization", "x-api-key"}
_UPSTREAM_REQUEST_SKIP = _HOP_BY_HOP | _CLIENT_AUTH_HEADERS | {
    "accept-encoding",
    "x-request-id",
    "x-token-router-request-id",
}
_POST_PATHS = {
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/responses",
}
_MAX_AUTH_TOKEN = 1024


@dataclass
class RouterApplication:
    config: Config
    db: Database
    quota: QuotaManager
    upstream: UpstreamClient
    started_monotonic: float
    semaphore: threading.BoundedSemaphore

    @property
    def uptime_seconds(self) -> int:
        return int(time.monotonic() - self.started_monotonic)

    def close(self) -> None:
        self.upstream.close()


class BadRequest(Exception):
    pass


class TokenRouterHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, server_address: tuple[str, int], app: RouterApplication) -> None:
        self.app = app
        super().__init__(server_address, TokenRouterHandler)


class TokenRouterHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"TokenRouter/{__version__}"
    sys_version = ""

    server: TokenRouterHTTPServer

    # -------------------------------------------------------------- HTTP verbs
    def do_GET(self) -> None:
        self._safe_dispatch()

    def do_HEAD(self) -> None:
        self._safe_dispatch()

    def do_POST(self) -> None:
        self._safe_dispatch()

    def do_PUT(self) -> None:
        self._safe_dispatch()

    def do_PATCH(self) -> None:
        self._safe_dispatch()

    def do_DELETE(self) -> None:
        self._safe_dispatch()

    def do_OPTIONS(self) -> None:
        self._safe_dispatch()

    def setup(self) -> None:
        super().setup()
        if self.server.app.config.request_timeout_seconds > 0:
            timeout = max(60.0, float(self.server.app.config.request_timeout_seconds))
            self.connection.settimeout(timeout)

    def finish(self) -> None:
        try:
            super().finish()
        finally:
            # ThreadingHTTPServer creates one thread per TCP connection, so this
            # is the right lifetime for the thread-local SQLite connection.
            self.server.app.db.close_thread_connection()

    # ---------------------------------------------------------------- dispatch
    def _safe_dispatch(self) -> None:
        try:
            self._dispatch()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True
        except Exception:
            logger.exception("unhandled request error path=%s", getattr(self, "path", "?"))
            if not getattr(self, "_headers_sent", False):
                self._send_json(
                    500,
                    _error_body("Internal server error", "server_error", "internal_error"),
                    close=True,
                )
            else:
                self.close_connection = True

    def _dispatch(self) -> None:
        app = self.server.app
        request_id = new_request_id()
        self.request_id = request_id
        self._headers_sent = False
        self._body_consumed = False
        self._reservation: Reservation | None = None
        self._settled = False
        self._started_wall = int(time.time())
        self._started_mono = time.monotonic()
        self._client_ip = self.client_address[0] if self.client_address else None
        self._model: str | None = None
        self._endpoint = ""
        self._key_name = "-"
        self._last_effective_usage = EMPTY_USAGE
        self._last_cost_micros = 0

        parsed = urlsplit(self.path)
        if parsed.scheme or parsed.netloc:
            self._send_json(
                400,
                _error_body("Absolute request targets are not supported", "invalid_request_error", "bad_request"),
                close=True,
            )
            return
        path = parsed.path
        query = parsed.query
        endpoint = canonical_path(path)
        self._endpoint = endpoint
        method = self.command.upper()

        if endpoint == "/health":
            if method in {"GET", "HEAD"}:
                has_body = bool(
                    self.headers.get("Transfer-Encoding")
                    or (self.headers.get("Content-Length") not in {None, "", "0"})
                )
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "version": __version__,
                        "uptime_seconds": app.uptime_seconds,
                    },
                    close=has_body,
                )
            else:
                self._send_json(
                    405,
                    _error_body("Method not allowed", "invalid_request_error", "method_not_allowed"),
                    close=True,
                    extra_headers={"Allow": "GET, HEAD"},
                )
            return

        if not app.semaphore.acquire(blocking=False):
            self._send_json(
                503,
                _error_body("Router is at capacity", "server_error", "server_busy"),
                close=True,
                extra_headers={"Retry-After": "1"},
            )
            return
        try:
            self._dispatch_authenticated(app, method, endpoint, path, query)
        finally:
            try:
                if self._reservation is not None and not self._settled:
                    self._settle(
                        status=499,
                        usage=EMPTY_USAGE,
                        error="request aborted before completion",
                    )
            finally:
                app.semaphore.release()

    def _dispatch_authenticated(
        self,
        app: RouterApplication,
        method: str,
        endpoint: str,
        path: str,
        query: str,
    ) -> None:
        if endpoint in _POST_PATHS:
            if method != "POST":
                self._send_json(
                    405,
                    _error_body("Method not allowed", "invalid_request_error", "method_not_allowed"),
                    close=True,
                    extra_headers={"Allow": "POST"},
                )
                return
        elif is_models_path(endpoint):
            if method not in {"GET", "HEAD"}:
                self._send_json(
                    405,
                    _error_body("Method not allowed", "invalid_request_error", "method_not_allowed"),
                    close=True,
                    extra_headers={"Allow": "GET, HEAD"},
                )
                return
        else:
            self._send_json(
                404,
                _error_body("Unknown endpoint", "invalid_request_error", "not_found"),
                close=True,
            )
            return

        key = self._authenticate(app)
        if key is None:
            return
        self._key_name = key.name
        if not key.enabled:
            self._send_json(
                403,
                _error_body("This API key is disabled", "permission_error", "key_disabled"),
                close=True,
            )
            return

        body = b""
        has_body_headers = bool(
            self.headers.get("Transfer-Encoding") or self.headers.get("Content-Length")
        )
        if method == "POST" or has_body_headers:
            try:
                body = self._read_body(app.config.max_request_bytes)
            except BadRequest as exc:
                self._send_json(
                    400,
                    _error_body(str(exc), "invalid_request_error", "bad_request"),
                    close=True,
                )
                return
            except TimeoutError:
                self._send_json(
                    408,
                    _error_body("Timed out while reading request body", "invalid_request_error", "request_timeout"),
                    close=True,
                )
                return
        if method == "POST" and not body:
            self._send_json(
                400,
                _error_body("Request body must be a JSON object", "invalid_request_error", "empty_body"),
                close=True,
            )
            return

        payload: dict[str, Any] | None = None
        if method == "POST":
            payload = parse_json_object(body)
            if payload is None:
                self._send_json(
                    400,
                    _error_body("Request body must be a valid JSON object", "invalid_request_error", "invalid_json"),
                    close=False,
                )
                return
            model = payload.get("model")
            self._model = model[:200] if isinstance(model, str) else None

        try:
            reservation = app.quota.reserve(key, payload, endpoint, now=self._started_wall)
        except QuotaExceeded as exc:
            violation = exc.violation
            retry_after = retry_after_seconds(violation.reset_at, self._started_wall)
            self._send_json(
                429,
                _error_body(
                    f"{'Daily' if violation.period == 'day' else 'Monthly'} "
                    f"{'token' if violation.kind == 'tokens' else 'cost'} quota exceeded for this API key",
                    "insufficient_quota",
                    "quota_exceeded",
                    details={**violation.details(), "retry_after_seconds": retry_after},
                ),
                close=False,
                extra_headers={"Retry-After": str(retry_after)},
            )
            return
        self._reservation = reservation

        upstream_body = body
        if method == "POST" and app.config.inject_stream_usage:
            upstream_body = inject_stream_usage(body, endpoint, enabled=True)

        headers = self._build_upstream_headers()
        try:
            exchange = app.upstream.open(method, path, query, headers, upstream_body if method == "POST" else None)
        except UpstreamError as exc:
            logger.warning("request=%s upstream_error=%s", self.request_id, exc)
            self._settle(status=502, usage=EMPTY_USAGE, error=str(exc))
            self._send_json(
                502,
                _error_body("Upstream service unavailable", "server_error", "upstream_unavailable"),
                close=False,
            )
            return

        try:
            content_type = exchange.response.getheader("Content-Type", "") or ""
            streaming = (
                200 <= exchange.status < 300
                and (
                    content_type.lower().startswith("text/event-stream")
                    or stream_requested(payload)
                )
            )
            if streaming:
                self._handle_streaming(app, payload, exchange)
            else:
                self._handle_buffered(app, payload, exchange)
        finally:
            exchange.close()

    def _authenticate(self, app: RouterApplication) -> ApiKey | None:
        token = _bearer_token(self.headers)
        if not token:
            self._send_json(
                401,
                _error_body("Missing API key", "authentication_error", "missing_api_key"),
                close=True,
                extra_headers={"WWW-Authenticate": 'Bearer realm="token-router"'},
            )
            return None
        if len(token) > _MAX_AUTH_TOKEN:
            self._send_json(
                401,
                _error_body("Invalid API key", "authentication_error", "invalid_api_key"),
                close=True,
                extra_headers={"WWW-Authenticate": 'Bearer realm="token-router"'},
            )
            return None
        key = app.db.get_key_by_hash(hash_api_key(token))
        if key is None:
            logger.warning("request=%s auth_failed ip=%s", self.request_id, self._client_ip)
            self._send_json(
                401,
                _error_body("Invalid API key", "authentication_error", "invalid_api_key"),
                close=True,
                extra_headers={"WWW-Authenticate": 'Bearer realm="token-router"'},
            )
            return None
        return key

    # ------------------------------------------------------------ body reading
    def _read_body(self, limit: int) -> bytes:
        transfer_encodings = self.headers.get_all("Transfer-Encoding") or []
        lengths = self.headers.get_all("Content-Length") or []
        if transfer_encodings and lengths:
            raise BadRequest("Both Transfer-Encoding and Content-Length are present")
        if transfer_encodings:
            if ",".join(transfer_encodings).strip().lower() != "chunked":
                raise BadRequest("Unsupported Transfer-Encoding")
            self._body_consumed = True
            return self._read_chunked(limit)
        if not lengths:
            self._body_consumed = True
            return b""
        normalized = {value.strip() for value in lengths}
        if len(normalized) != 1:
            raise BadRequest("Conflicting Content-Length headers")
        raw_length = next(iter(normalized))
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise BadRequest("Invalid Content-Length") from exc
        if length < 0:
            raise BadRequest("Invalid Content-Length")
        if length > limit:
            raise BadRequest(f"Request body exceeds {limit} bytes")
        self._body_consumed = True
        data = self.rfile.read(length)
        if len(data) != length:
            raise BadRequest("Request body ended prematurely")
        return data

    def _read_chunked(self, limit: int) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            line = self.rfile.readline(65536)
            if not line or not line.endswith(b"\n"):
                raise BadRequest("Malformed chunked body")
            size_text = line.strip().split(b";", 1)[0]
            try:
                size = int(size_text, 16)
            except ValueError as exc:
                raise BadRequest("Malformed chunk size") from exc
            if size < 0:
                raise BadRequest("Malformed chunk size")
            if size == 0:
                trailer_size = 0
                while True:
                    trailer = self.rfile.readline(65536)
                    if not trailer or not trailer.endswith(b"\n"):
                        raise BadRequest("Malformed chunk trailer")
                    trailer_size += len(trailer)
                    if trailer_size > 65536:
                        raise BadRequest("Chunk trailer too large")
                    if trailer in {b"\r\n", b"\n"}:
                        break
                break
            total += size
            if total > limit:
                raise BadRequest(f"Request body exceeds {limit} bytes")
            chunk = self.rfile.read(size)
            if len(chunk) != size:
                raise BadRequest("Chunk ended prematurely")
            ending = self.rfile.read(2)
            if ending != b"\r\n":
                raise BadRequest("Malformed chunk terminator")
            chunks.append(chunk)
        return b"".join(chunks)

    # -------------------------------------------------------------- buffered I/O
    def _handle_buffered(
        self,
        app: RouterApplication,
        payload: dict[str, Any] | None,
        exchange: UpstreamExchange,
    ) -> None:
        try:
            body = exchange.read_all(app.config.max_response_bytes)
        except ResponseTooLarge as exc:
            logger.warning("request=%s response_too_large=%s", self.request_id, exc)
            self._settle(status=502, usage=EMPTY_USAGE, error=str(exc))
            self._send_json(
                502,
                _error_body("Upstream response too large", "server_error", "response_too_large"),
                close=False,
            )
            return
        except (OSError, http.client.HTTPException, TimeoutError) as exc:
            logger.warning("request=%s upstream_read_error=%s", self.request_id, exc)
            self._settle(status=502, usage=EMPTY_USAGE, error=f"upstream read: {exc}")
            if not self._headers_sent:
                self._send_json(
                    502,
                    _error_body("Upstream connection failed", "server_error", "upstream_error"),
                    close=False,
                )
            else:
                self.close_connection = True
            return
        finally:
            exchange.release()

        status = exchange.status
        usage = EMPTY_USAGE
        error: str | None = None
        if 200 <= status < 300:
            response_json = None
            encoding = (exchange.response.getheader("Content-Encoding") or "").lower()
            if encoding in {"", "identity"}:
                response_json = parse_json_object(body)
            reported = extract_usage(response_json) if response_json is not None else None
            if reported is not None:
                usage = reported
            elif is_billable_path(self._endpoint):
                usage = fallback_usage(payload, self._endpoint, response_json)
            self._settle(status=status, usage=usage, error=None)
        else:
            error = f"upstream status {status}"
            self._settle(status=status, usage=EMPTY_USAGE, error=error)
        self._send_upstream_buffered(exchange, body)

    def _send_upstream_buffered(self, exchange: UpstreamExchange, body: bytes) -> None:
        self.send_response(exchange.status, exchange.reason)
        self._headers_sent = True
        for header, value in exchange.headers:
            self._copy_upstream_header(header, value)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Token-Router-Request-Id", self.request_id)
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()
        if self.command != "HEAD" and body:
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
        self._log_access(exchange.status)

    def _copy_upstream_header(self, header: str, value: str) -> bool:
        lower = header.lower()
        if lower in _HOP_BY_HOP or lower == "x-token-router-request-id":
            return False
        if any(char in header for char in "\r\n") or any(char in value for char in "\r\n"):
            logger.warning("request=%s dropped_unsafe_upstream_header=%s", self.request_id, header)
            return False
        self.send_header(header, value)
        return True

    # -------------------------------------------------------------- streaming I/O
    def _handle_streaming(
        self,
        app: RouterApplication,
        payload: dict[str, Any] | None,
        exchange: UpstreamExchange,
    ) -> None:
        from .usage import SSEUsageExtractor

        self.send_response(exchange.status, exchange.reason)
        self._headers_sent = True
        content_type_sent = False
        for header, value in exchange.headers:
            if not self._copy_upstream_header(header, value):
                continue
            if header.lower() == "content-type":
                content_type_sent = True
        if not content_type_sent:
            self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("X-Token-Router-Request-Id", self.request_id)
        if not exchange.response.getheader("Cache-Control"):
            self.send_header("Cache-Control", "no-cache")
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

        extractor = SSEUsageExtractor()
        prefix = bytearray()
        total = 0
        client_disconnected = False
        read_error: str | None = None
        upstream_complete = False
        try:
            while True:
                try:
                    chunk = exchange.read(app.config.stream_chunk_size)
                except (OSError, http.client.HTTPException, TimeoutError) as exc:
                    read_error = f"upstream read: {exc}"
                    break
                if not chunk:
                    upstream_complete = True
                    break
                total += len(chunk)
                if total > app.config.max_response_bytes:
                    read_error = "upstream stream exceeded max_response_bytes"
                    break
                if len(prefix) < 65536:
                    prefix.extend(chunk[: 65536 - len(prefix)])
                extractor.feed(chunk)
                try:
                    self._write_chunk(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    client_disconnected = True
                    break
        finally:
            if upstream_complete:
                extractor.finish()
            exchange.release() if upstream_complete else exchange.close()

        if client_disconnected:
            usage = self._stream_usage(payload, extractor, prefix)
            self._settle(status=200, usage=usage, error="client disconnected")
            self.close_connection = True
            self._log_access(200, note="client-disconnected")
            return

        usage = self._stream_usage(payload, extractor, prefix)
        status = exchange.status if upstream_complete else 200
        self._settle(status=status, usage=usage, error=read_error)
        self._log_access(status, note=read_error or "")

        if not upstream_complete:
            # We already sent a 200 and partial SSE data. A clean terminal chunk
            # would falsely signal success, so drop the connection instead.
            self.close_connection = True
            return

        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            self.close_connection = True

    def _stream_usage(
        self,
        payload: dict[str, Any] | None,
        extractor: Any,
        prefix: bytearray,
    ) -> TokenUsage:
        if extractor.usage is not None:
            return extractor.usage
        if is_billable_path(self._endpoint):
            return fallback_stream_usage(payload, extractor, bytes(prefix))
        return EMPTY_USAGE

    def _write_chunk(self, data: bytes) -> None:
        self.wfile.write(f"{len(data):X}\r\n".encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    # --------------------------------------------------------------- utilities
    def _build_upstream_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {}
        for header, value in self.headers.items():
            lower = header.lower()
            if lower in _UPSTREAM_REQUEST_SKIP:
                continue
            headers[header] = value
        if self.command == "POST":
            headers.setdefault("Content-Type", "application/json")
        headers["X-Request-Id"] = self.request_id
        return headers

    def _settle(
        self,
        *,
        status: int,
        usage: TokenUsage,
        error: str | None,
        timestamp: int | None = None,
    ) -> None:
        if self._reservation is None or self._settled:
            return
        self._settled = True
        latency_ms = int((time.monotonic() - self._started_mono) * 1000)
        try:
            effective = self.server.app.quota.settle(
                self._reservation,
                request_id=self.request_id,
                endpoint=self._endpoint,
                model=self._model,
                status=status,
                usage=usage,
                latency_ms=latency_ms,
                client_ip=self._client_ip,
                error=error,
                timestamp=timestamp,
            )
            self._last_effective_usage = effective.usage
            self._last_cost_micros = effective.cost_micros
        except Exception:
            # A ledger failure must not crash a request after data was sent.
            logger.exception("request=%s ledger_write_failed", self.request_id)
            self._last_effective_usage = usage

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        close: bool,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self._headers_sent = True
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Token-Router-Request-Id", getattr(self, "request_id", ""))
        if extra_headers:
            for header, value in extra_headers.items():
                self.send_header(header, value)
        if close:
            self.send_header("Connection", "close")
            self.close_connection = True
        self.end_headers()
        if self.command != "HEAD":
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                self.close_connection = True
        if status >= 400:
            self._log_access(status)

    def _log_access(self, status: int, *, note: str = "") -> None:
        usage = getattr(self, "_last_effective_usage", None) or EMPTY_USAGE
        key = getattr(self, "_key_name", "-")
        latency_ms = int((time.monotonic() - self._started_mono) * 1000)
        cost = format_amount(
            getattr(self, "_last_cost_micros", 0),
            self.server.app.config.currency_symbol,
        )
        logger.info(
            "request=%s key=%s endpoint=%s status=%s in=%d out=%d cost=%s est=%s "
            "latency_ms=%d ip=%s%s",
            getattr(self, "request_id", "-"),
            key,
            getattr(self, "_endpoint", "-"),
            status,
            usage.input_tokens,
            usage.output_tokens,
            cost,
            usage.estimated,
            latency_ms,
            getattr(self, "_client_ip", "-"),
            f" note={note}" if note else "",
        )

    def log_message(self, format: str, *args: Any) -> None:
        # Suppress BaseHTTPRequestHandler's unstructured stderr logging.
        logger.debug("http: " + format, *args)


def _error_body(
    message: str,
    error_type: str,
    code: str,
    *,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {
        "message": message,
        "type": error_type,
        "param": None,
        "code": code,
    }
    if details:
        error["details"] = details
    return {"error": error}


def _bearer_token(headers: Any) -> str | None:
    authorization = headers.get("Authorization")
    if authorization:
        parts = authorization.strip().split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            return parts[1].strip()
        return None
    api_key = headers.get("X-Api-Key")
    if api_key:
        return api_key.strip()
    return None


def build_application(config: Config) -> RouterApplication:
    db = Database(config.database_path, config.timezone_offset_minutes)
    db.initialize()
    upstream_key = config.require_upstream_api_key()
    upstream = UpstreamClient(config, upstream_key)
    quota = QuotaManager(db, config)
    return RouterApplication(
        config=config,
        db=db,
        quota=quota,
        upstream=upstream,
        started_monotonic=time.monotonic(),
        semaphore=threading.BoundedSemaphore(config.max_concurrency),
    )


def make_server(app: RouterApplication) -> TokenRouterHTTPServer:
    if ":" in app.config.host:
        class IPv6TokenRouterHTTPServer(TokenRouterHTTPServer):
            address_family = socket.AF_INET6

        server_class: type[TokenRouterHTTPServer] = IPv6TokenRouterHTTPServer
    else:
        server_class = TokenRouterHTTPServer
    return server_class((app.config.host, app.config.port), app)


def run_server(config: Config) -> None:
    app = build_application(config)
    server = make_server(app)
    bound_host, bound_port = server.server_address[:2]
    logger.info(
        "TokenRouter %s listening on http://%s:%s (upstream=%s, db=%s)",
        __version__,
        bound_host,
        bound_port,
        config.base_url_description,
        config.database_path,
    )
    if not config.is_loopback:
        logger.warning("绑定到了非回环地址，局域网内其他主机也可能访问此网关")
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在关闭")
    finally:
        server.server_close()
        app.close()
