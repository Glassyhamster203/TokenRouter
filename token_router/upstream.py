"""A tiny HTTP/1.1 upstream client with a bounded keep-alive connection pool."""

from __future__ import annotations

import http.client
import select
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Mapping
from urllib.parse import quote, urlparse

from .config import Config


class UpstreamError(RuntimeError):
    """Raised before a response is available (connect/protocol/network error)."""


class ResponseTooLarge(UpstreamError):
    pass


@dataclass(frozen=True)
class PoolStats:
    idle: int
    created: int


class UpstreamExchange:
    """One request/response pair; owns the connection until released."""

    def __init__(
        self,
        client: "UpstreamClient",
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        self._client = client
        self.connection = connection
        self.response = response
        self._released = False

    @property
    def status(self) -> int:
        return int(self.response.status)

    @property
    def reason(self) -> str:
        return str(self.response.reason or "")

    @property
    def headers(self) -> list[tuple[str, str]]:
        return [(str(key), str(value)) for key, value in self.response.getheaders()]

    def read(self, amount: int = 65536) -> bytes:
        return self.response.read(amount)

    def read_all(self, limit: int) -> bytes:
        length_header = self.response.getheader("Content-Length")
        if length_header:
            try:
                if int(length_header) > limit:
                    raise ResponseTooLarge(
                        f"上游响应体超过限制 {limit} 字节（Content-Length={length_header}）"
                    )
            except ValueError:
                pass
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = self.response.read(min(65536, limit + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ResponseTooLarge(f"上游响应体超过限制 {limit} 字节")
            chunks.append(chunk)
        return b"".join(chunks)

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._client._release(self.connection, self.response)

    def close(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            self.response.close()
        finally:
            try:
                self.connection.close()
            except OSError:
                pass


class UpstreamClient:
    def __init__(self, config: Config, api_key: str) -> None:
        parsed = urlparse(config.upstream_base_url)
        self.config = config
        self.api_key = api_key
        self.scheme = parsed.scheme
        self.host = parsed.hostname or ""
        self.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self.base_path = parsed.path.rstrip("/")
        self.base_path_is_v1 = self.base_path.endswith("/v1") or self.base_path == "/v1"
        self._idle: list[tuple[http.client.HTTPConnection, float]] = []
        self._lock = threading.Lock()
        self._created = 0
        self._ssl_context = self._build_ssl_context() if self.scheme == "https" else None

    def _build_ssl_context(self) -> ssl.SSLContext:
        if not self.config.verify_upstream_tls:
            context = ssl._create_unverified_context()  # noqa: SLF001 - stdlib API
        else:
            cafile = str(self.config.upstream_ca_file) if self.config.upstream_ca_file else None
            context = ssl.create_default_context(cafile=cafile)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context

    def map_target(self, incoming_path: str, query: str = "") -> str:
        """Map downstream ``/v1/...`` paths onto a configurable base URL."""

        if incoming_path.startswith("/v1/"):
            suffix = incoming_path[3:]
        elif incoming_path == "/v1":
            suffix = ""
        else:
            suffix = incoming_path
        if self.base_path_is_v1:
            target_path = self.base_path + suffix
        else:
            target_path = self.base_path + incoming_path
        if not target_path:
            target_path = "/"
        target_path = quote(target_path, safe="/%:@!$&'()*+,;=-._~")
        if query:
            target = target_path + "?" + query
        else:
            target = target_path
        return target

    def open(
        self,
        method: str,
        incoming_path: str,
        query: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> UpstreamExchange:
        connection = self._acquire()
        target = self.map_target(incoming_path, query)
        request_headers = dict(headers)
        # Configured headers (for example OpenAI-Organization) win over
        # client-supplied headers, then the managed auth header wins over all.
        for header, value in self.config.upstream_headers.items():
            for existing in [name for name in request_headers if name.lower() == header.lower()]:
                del request_headers[existing]
            request_headers[header] = value
        request_headers["Accept-Encoding"] = "identity"
        request_headers["Connection"] = "keep-alive"
        request_headers[self.config.upstream_auth_header] = (
            f"{self.config.upstream_auth_scheme}{self.api_key}"
        )
        try:
            if connection.sock is not None:
                connection.sock.settimeout(self.config.request_timeout_seconds)
            connection.request(method, target, body=body, headers=request_headers)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException, socket.timeout) as exc:
            try:
                connection.close()
            except OSError:
                pass
            raise UpstreamError(f"上游请求失败：{type(exc).__name__}: {exc}") from exc
        return UpstreamExchange(self, connection, response)

    def _acquire(self) -> http.client.HTTPConnection:
        now = time.monotonic()
        while True:
            with self._lock:
                if not self._idle:
                    break
                connection, released_at = self._idle.pop()
            stale = now - released_at > self.config.upstream_pool_idle_seconds
            if stale or connection.sock is None or _socket_has_pending_data(connection.sock):
                connection.close()
                continue
            return connection

        if self.scheme == "https":
            connection = http.client.HTTPSConnection(
                self.host,
                self.port,
                timeout=self.config.connect_timeout_seconds,
                context=self._ssl_context,
            )
        else:
            connection = http.client.HTTPConnection(
                self.host,
                self.port,
                timeout=self.config.connect_timeout_seconds,
            )
        try:
            connection.connect()
            if connection.sock is not None:
                connection.sock.settimeout(self.config.request_timeout_seconds)
        except (OSError, ssl.SSLError, socket.timeout) as exc:
            connection.close()
            raise UpstreamError(f"无法连接上游：{type(exc).__name__}: {exc}") from exc
        with self._lock:
            self._created += 1
        return connection

    def _release(
        self,
        connection: http.client.HTTPConnection,
        response: http.client.HTTPResponse,
    ) -> None:
        reusable = (
            response.isclosed()
            and not response.will_close
            and connection.sock is not None
        )
        if not reusable:
            try:
                response.close()
            finally:
                connection.close()
            return
        with self._lock:
            if len(self._idle) >= self.config.upstream_pool_size:
                connection.close()
                return
            self._idle.append((connection, time.monotonic()))

    def close(self) -> None:
        with self._lock:
            idle, self._idle = self._idle, []
        for connection, _ in idle:
            connection.close()

    def stats(self) -> PoolStats:
        with self._lock:
            return PoolStats(idle=len(self._idle), created=self._created)


def _socket_has_pending_data(sock: socket.socket) -> bool:
    """Detect a peer that closed an idle keep-alive connection."""

    try:
        readable, _, exceptional = select.select([sock], [], [sock], 0)
        return bool(readable or exceptional)
    except (OSError, ValueError):
        return True
