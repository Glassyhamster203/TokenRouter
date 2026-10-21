"""Token estimation, provider usage extraction, and SSE usage tapping."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .util import estimate_text_tokens

_CHAT_PATHS = {"/v1/chat/completions", "/v1/completions"}
_BILLABLE_PATHS = {
    "/v1/chat/completions",
    "/v1/completions",
    "/v1/embeddings",
    "/v1/responses",
}
_MAX_SSE_LINE = 4 * 1024 * 1024
_MAX_SSE_CAPTURE_TEXT = 2 * 1024 * 1024


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    estimated: bool = False

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "total_tokens": self.total_tokens,
            "estimated": self.estimated,
        }


EMPTY_USAGE = TokenUsage()


def canonical_path(path: str) -> str:
    if not path:
        return "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return path


def is_billable_path(path: str) -> bool:
    return canonical_path(path) in _BILLABLE_PATHS


def is_embeddings_path(path: str) -> bool:
    return canonical_path(path) == "/v1/embeddings"


def is_models_path(path: str) -> bool:
    normalized = canonical_path(path)
    return normalized == "/v1/models" or normalized.startswith("/v1/models/")


def _first_int(mapping: dict[str, Any], *keys: str) -> int | None:
    for key in keys:
        value = mapping.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, float) and value >= 0 and value.is_integer():
            return int(value)
    return None


def extract_usage(value: Any) -> TokenUsage | None:
    """Extract input/output token counts from an OpenAI-style object."""

    if not isinstance(value, dict):
        return None
    candidates: list[dict[str, Any]] = []
    usage = value.get("usage")
    if isinstance(usage, dict):
        candidates.append(usage)
    response = value.get("response")
    if isinstance(response, dict) and isinstance(response.get("usage"), dict):
        candidates.append(response["usage"])
    for candidate in candidates:
        prompt = _first_int(candidate, "prompt_tokens", "input_tokens")
        completion = _first_int(candidate, "completion_tokens", "output_tokens")
        total = _first_int(candidate, "total_tokens")
        if prompt is None and completion is None:
            if total is None:
                continue
            prompt, completion = total, 0
        prompt = prompt or 0
        completion = completion or 0
        if total is not None and prompt + completion == 0 and total > 0:
            prompt, completion = total, 0
        return TokenUsage(prompt, completion, estimated=False)
    return None


def parse_json_bytes(body: bytes) -> Any | None:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def parse_json_object(body: bytes) -> dict[str, Any] | None:
    value = parse_json_bytes(body)
    return value if isinstance(value, dict) else None


def _part_text(part: Any, image_counter: list[int]) -> str:
    if isinstance(part, str):
        return part
    if not isinstance(part, dict):
        return ""
    part_type = str(part.get("type", ""))
    if part_type in {"image_url", "input_image", "image"}:
        image_counter[0] += 1
        return ""
    if "text" in part and isinstance(part["text"], str):
        return part["text"]
    # Tool outputs, audio transcripts and provider-specific text fields.
    pieces: list[str] = []
    for key in ("content", "input_text", "output_text", "transcript"):
        item = part.get(key)
        if isinstance(item, str):
            pieces.append(item)
        elif isinstance(item, list):
            pieces.extend(_part_text(child, image_counter) for child in item)
    return "\n".join(piece for piece in pieces if piece)


def _content_text(content: Any, image_counter: list[int]) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(filter(None, (_part_text(part, image_counter) for part in content)))
    if isinstance(content, dict):
        return _part_text(content, image_counter)
    return ""


def _message_text(message: Any, image_counter: list[int]) -> str:
    if not isinstance(message, dict):
        return _content_text(message, image_counter)
    pieces: list[str] = []
    role = message.get("role")
    if isinstance(role, str):
        pieces.append(role)
    pieces.append(_content_text(message.get("content"), image_counter))
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list):
        for call in tool_calls:
            if isinstance(call, dict):
                function = call.get("function")
                if isinstance(function, dict):
                    if isinstance(function.get("name"), str):
                        pieces.append(function["name"])
                    if isinstance(function.get("arguments"), str):
                        pieces.append(function["arguments"])
    for key in ("name", "tool_call_id"):
        if isinstance(message.get(key), str):
            pieces.append(message[key])
    return "\n".join(piece for piece in pieces if piece)


def _collect_prompt_text(payload: dict[str, Any]) -> tuple[str, int]:
    pieces: list[str] = []
    image_counter = [0]
    messages = payload.get("messages")
    if isinstance(messages, list):
        pieces.extend(_message_text(message, image_counter) for message in messages)
    for key in ("prompt", "input", "instructions", "system"):
        value = payload.get(key)
        if isinstance(value, str):
            pieces.append(value)
        elif isinstance(value, list):
            if value and all(isinstance(item, dict) and "role" in item for item in value):
                pieces.extend(_message_text(item, image_counter) for item in value)
            else:
                pieces.extend(_part_text(item, image_counter) for item in value)
        elif isinstance(value, dict):
            pieces.append(_message_text(value, image_counter))
    for key in ("tools", "functions"):
        value = payload.get(key)
        if value is not None:
            pieces.append(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(piece for piece in pieces if piece), image_counter[0]


def estimate_prompt_tokens(payload: dict[str, Any]) -> int:
    text, images = _collect_prompt_text(payload)
    # Vision pricing is model-specific. A small fixed allowance protects quota
    # enforcement without making base64 image payloads look like millions of
    # input tokens.
    return estimate_text_tokens(text) + images * 256


def requested_output_tokens(payload: dict[str, Any], path: str, default: int) -> int:
    if is_embeddings_path(path):
        return 0
    candidates = [
        payload.get("max_completion_tokens"),
        payload.get("max_tokens"),
        payload.get("max_output_tokens"),
    ]
    text_config = payload.get("text")
    if isinstance(text_config, dict):
        candidates.append(text_config.get("max_output_tokens"))
    generation_config = payload.get("generation_config")
    if isinstance(generation_config, dict):
        candidates.append(generation_config.get("max_output_tokens"))
    for value in candidates:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and 0 < value <= 1_000_000:
            return value
    return max(1, default)


def estimate_output_tokens_from_json(payload: Any) -> int:
    return estimate_text_tokens(_output_text_from_object(payload))


def _output_text_from_object(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    pieces: list[str] = []
    choices = value.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if isinstance(message, dict):
                pieces.append(_content_text(message.get("content"), [0]))
                pieces.append(_content_text(message.get("reasoning_content"), [0]))
            for key in ("text", "delta", "content"):
                item = choice.get(key)
                if isinstance(item, str):
                    pieces.append(item)
                elif isinstance(item, dict):
                    pieces.append(_content_text(item.get("content"), [0]))
    output_text = value.get("output_text")
    if isinstance(output_text, str):
        pieces.append(output_text)
    output = value.get("output")
    if isinstance(output, list):
        for item in output:
            if isinstance(item, dict):
                pieces.append(_content_text(item.get("content"), [0]))
    return "".join(pieces)


def _output_text_from_sse_event(value: dict[str, Any]) -> str:
    event_type = str(value.get("type", ""))
    if event_type == "response.output_text.delta":
        delta = value.get("delta")
        return delta if isinstance(delta, str) else ""
    pieces: list[str] = []
    choices = value.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                pieces.append(_content_text(delta.get("content"), [0]))
                pieces.append(_content_text(delta.get("reasoning_content"), [0]))
            text = choice.get("text")
            if isinstance(text, str):
                pieces.append(text)
    return "".join(pieces)


def inject_stream_usage(body: bytes, path: str, *, enabled: bool) -> bytes:
    """Ask the upstream to emit a final SSE event carrying exact usage.

    Only OpenAI chat/completion streams support ``stream_options``. Some
    OpenAI-compatible servers leave the extra event out, so billing still has a
    token-estimation fallback.
    """

    if not enabled or canonical_path(path) not in _CHAT_PATHS:
        return body
    payload = parse_json_object(body)
    if payload is None or payload.get("stream") is not True:
        return body
    stream_options = payload.get("stream_options")
    if not isinstance(stream_options, dict):
        stream_options = {}
    if stream_options.get("include_usage") is True:
        return body
    stream_options["include_usage"] = True
    payload["stream_options"] = stream_options
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def stream_requested(payload: dict[str, Any] | None) -> bool:
    return bool(payload and payload.get("stream") is True)


class SSEUsageExtractor:
    """Incrementally tap SSE bytes while the proxy forwards them verbatim."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._data: list[bytes] = []
        self._event_data_size = 0
        self.usage: TokenUsage | None = None
        self._output_parts: list[str] = []
        self._output_size = 0
        self._truncated = False
        self.event_count = 0
        self.malformed_events = 0

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        self._buffer.extend(chunk)
        while True:
            newline = self._buffer.find(b"\n")
            if newline < 0:
                if len(self._buffer) > _MAX_SSE_LINE:
                    # Do not let a hostile/broken event consume unbounded RAM.
                    self._buffer.clear()
                    self._data.clear()
                    self._event_data_size = 0
                    self._truncated = True
                return
            line = bytes(self._buffer[:newline])
            del self._buffer[: newline + 1]
            if line.endswith(b"\r"):
                line = line[:-1]
            self._process_line(line)

    def finish(self) -> None:
        if self._buffer:
            line = bytes(self._buffer)
            self._buffer.clear()
            if line.endswith(b"\r"):
                line = line[:-1]
            self._process_line(line)
        self._dispatch_event()

    def _process_line(self, line: bytes) -> None:
        if not line:
            self._dispatch_event()
            return
        if line.startswith(b":"):
            return
        if line.startswith(b"data:"):
            payload = line[5:]
            if payload.startswith(b" "):
                payload = payload[1:]
            self._event_data_size += len(payload)
            if self._event_data_size > _MAX_SSE_LINE:
                self._truncated = True
                self._data.clear()
                self._event_data_size = 0
                return
            self._data.append(payload)

    def _dispatch_event(self) -> None:
        if not self._data:
            return
        raw = b"\n".join(self._data)
        self._data.clear()
        self._event_data_size = 0
        if raw.strip() == b"[DONE]":
            return
        self.event_count += 1
        value = parse_json_bytes(raw)
        if not isinstance(value, dict):
            self.malformed_events += 1
            return
        found = extract_usage(value)
        if found is not None:
            self.usage = found
        text = _output_text_from_sse_event(value)
        if text:
            if self._output_size < _MAX_SSE_CAPTURE_TEXT:
                remaining = _MAX_SSE_CAPTURE_TEXT - self._output_size
                captured = text[:remaining]
                self._output_parts.append(captured)
                self._output_size += len(captured)
            else:
                self._truncated = True

    @property
    def estimated_output_tokens(self) -> int:
        return estimate_text_tokens("".join(self._output_parts))

    @property
    def truncated(self) -> bool:
        return self._truncated


def fallback_usage(payload: dict[str, Any] | None, path: str, response_json: Any = None) -> TokenUsage:
    input_tokens = estimate_prompt_tokens(payload) if payload else 0
    if is_embeddings_path(path):
        output_tokens = 0
    elif response_json is not None:
        output_tokens = estimate_output_tokens_from_json(response_json)
    else:
        output_tokens = 0
    return TokenUsage(input_tokens, output_tokens, estimated=True)


def fallback_stream_usage(
    payload: dict[str, Any] | None,
    extractor: SSEUsageExtractor,
    partial: bytes = b"",
) -> TokenUsage:
    input_tokens = estimate_prompt_tokens(payload) if payload else 0
    output_tokens = extractor.estimated_output_tokens
    if output_tokens == 0 and partial:
        # If the stream had no parseable text chunks, estimate from a bounded
        # prefix of the raw bytes rather than billing nothing.
        output_tokens = estimate_text_tokens(partial[:65536].decode("utf-8", "ignore"))
    return TokenUsage(input_tokens, output_tokens, estimated=True)
