"""Configuration loading and validation for TokenRouter.

Configuration can be JSON or TOML. The upstream API key may come from an
environment variable (recommended for services) or the configuration file
(convenient for local/file-driven deployments).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .util import (
    UserError,
    format_amount,
    is_loopback_host,
    parse_money_to_micros,
    parse_price_to_micros_per_mtok,
    parse_token_quantity,
)


CONFIG_ENV = "TR_CONFIG"
UPSTREAM_KEY_ENV = "TR_UPSTREAM_API_KEY"
DEFAULT_CONFIG_FILENAME = "config.json"

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR"}
_FORBIDDEN_UPSTREAM_HEADERS = {
    "host",
    "authorization",
    "connection",
    "content-length",
    "transfer-encoding",
    "accept-encoding",
    "upgrade",
    "proxy-authorization",
    "proxy-authenticate",
}
_USER_FIELDS = {
    "name",
    "api_key",
    "key",
    "daily_token_limit",
    "monthly_token_limit",
    "daily_cost_limit",
    "monthly_cost_limit",
    "allowed_models",
    "enabled",
    "note",
}
_MISSING = object()


class ConfigError(UserError):
    pass


@dataclass(frozen=True)
class ConfigUser:
    """A downstream user declared in the JSON/TOML configuration file."""

    name: str
    api_key: str
    daily_token_limit: int | None = None
    monthly_token_limit: int | None = None
    daily_cost_limit_micros: int | None = None
    monthly_cost_limit_micros: int | None = None
    # None = unrestricted, () = deny every model, tuple = allowed patterns.
    allowed_models: tuple[str, ...] | None = None
    enabled: bool = True
    note: str = ""


@dataclass(frozen=True)
class Config:
    host: str = "127.0.0.1"
    port: int = 8787
    upstream_base_url: str = "https://api.openai.com/v1"
    upstream_api_key_env: str = "TOKEN_ROUTER_UPSTREAM_KEY"
    upstream_api_key_value: str | None = None
    upstream_auth_header: str = "Authorization"
    upstream_auth_scheme: str = "Bearer "
    upstream_headers: dict[str, str] = field(default_factory=dict)
    data_dir: Path = Path("data")
    database_file: str = "router.db"
    timezone_offset_minutes: int = 480
    currency: str = "USD"
    currency_symbol: str = "$"
    default_price_in_micros_per_mtok: int = 0
    default_price_out_micros_per_mtok: int = 0
    default_max_output_tokens: int = 4096
    max_request_bytes: int = 16 * 1024 * 1024
    max_response_bytes: int = 64 * 1024 * 1024
    max_concurrency: int = 128
    connect_timeout_seconds: float = 10.0
    request_timeout_seconds: float = 300.0
    upstream_pool_size: int = 32
    upstream_pool_idle_seconds: float = 30.0
    stream_chunk_size: int = 64 * 1024
    inject_stream_usage: bool = True
    verify_upstream_tls: bool = True
    upstream_ca_file: Path | None = None
    log_level: str = "INFO"
    users: tuple[ConfigUser, ...] = ()
    config_path: Path | None = None
    base_dir: Path = field(default_factory=Path.cwd)
    config_format: str = "json"

    @property
    def database_path(self) -> Path:
        return self.data_dir / self.database_file

    @property
    def upstream_api_key(self) -> str | None:
        for env_name in (UPSTREAM_KEY_ENV, self.upstream_api_key_env):
            value = os.environ.get(env_name)
            if value and value.strip():
                return value.strip()
        return self.upstream_api_key_value

    @property
    def upstream_api_key_source(self) -> str:
        for env_name in (UPSTREAM_KEY_ENV, self.upstream_api_key_env):
            value = os.environ.get(env_name)
            if value and value.strip():
                return f"env:{env_name}"
        return "file" if self.upstream_api_key_value else "missing"

    def require_upstream_api_key(self) -> str:
        key = self.upstream_api_key
        if not key:
            raise ConfigError(
                "未设置上游 API key。请在配置文件中设置 upstream_api_key，"
                f"或设置环境变量 {UPSTREAM_KEY_ENV} / {self.upstream_api_key_env}。"
            )
        return key

    def with_overrides(self, **kwargs: Any) -> "Config":
        return replace(self, **kwargs)

    def public_view(self) -> dict[str, Any]:
        key = self.upstream_api_key
        return {
            "config_path": str(self.config_path) if self.config_path else "(defaults)",
            "config_format": self.config_format,
            "host": self.host,
            "port": self.port,
            "upstream_base_url": self.upstream_base_url,
            "upstream_api_key": "set" if key else "MISSING",
            "upstream_api_key_source": self.upstream_api_key_source,
            "database_path": str(self.database_path),
            "timezone_offset_minutes": self.timezone_offset_minutes,
            "currency": self.currency,
            "max_concurrency": self.max_concurrency,
            "request_timeout_seconds": self.request_timeout_seconds,
            "inject_stream_usage": self.inject_stream_usage,
            "verify_upstream_tls": self.verify_upstream_tls,
            "configured_users": len(self.users),
        }

    def format_price(self, micros_per_mtok: int) -> str:
        return f"{format_amount(micros_per_mtok, self.currency_symbol)}/M"

    @property
    def is_loopback(self) -> bool:
        return is_loopback_host(self.host)

    @property
    def base_url_description(self) -> str:
        parsed = urlparse(self.upstream_base_url)
        host = parsed.hostname or parsed.netloc
        if parsed.port:
            host = f"{host}:{parsed.port}"
        return f"{parsed.scheme}://{host}{parsed.path}"


def resolve_config_path(explicit: str | Path | None = None) -> Path:
    if explicit:
        candidate = Path(explicit).expanduser()
    elif os.environ.get(CONFIG_ENV):
        candidate = Path(os.environ[CONFIG_ENV]).expanduser()
    else:
        candidate = Path(DEFAULT_CONFIG_FILENAME)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    return candidate.resolve()


def load_config(
    path: str | Path | None = None,
    *,
    require_exists: bool = False,
) -> Config:
    config_path = resolve_config_path(path)
    document: dict[str, Any] = {}
    config_format = _config_format(config_path)
    if config_path.exists():
        if not config_path.is_file():
            raise ConfigError(f"配置文件不是普通文件：{config_path}")
        document = _read_config_document(config_path, config_format)
    elif require_exists:
        raise ConfigError(f"配置文件不存在：{config_path}")

    allowed = {
        name
        for name in Config.__dataclass_fields__
        if name not in {"config_path", "base_dir", "config_format", "upstream_api_key_value"}
    }
    allowed.add("upstream_api_key")
    unknown = set(document) - allowed
    if unknown:
        raise ConfigError(f"配置文件包含未知字段：{', '.join(sorted(unknown))}")

    values: dict[str, Any] = dict(document)
    _apply_environment(values)

    base_dir = config_path.parent
    data_dir_value = values.get("data_dir", "data")
    data_dir = _resolve_path(data_dir_value, base_dir, field_name="data_dir")
    ca_value = values.get("upstream_ca_file")
    ca_file = _resolve_path(ca_value, base_dir, field_name="upstream_ca_file") if ca_value else None
    upstream_key = _as_optional_str(values, "upstream_api_key")
    if upstream_key is not None:
        upstream_key = upstream_key.strip()
        if not upstream_key:
            upstream_key = None
        elif any(char in upstream_key for char in "\r\n\x00"):
            raise ConfigError("upstream_api_key 不能包含换行或空字符")
        elif len(upstream_key) > 4096:
            raise ConfigError("upstream_api_key 过长")

    config = Config(
        host=_as_str(values, "host", "127.0.0.1"),
        port=_as_int(values, "port", 8787),
        upstream_base_url=_as_str(values, "upstream_base_url", "https://api.openai.com/v1"),
        upstream_api_key_env=_as_str(values, "upstream_api_key_env", "TOKEN_ROUTER_UPSTREAM_KEY"),
        upstream_api_key_value=upstream_key,
        upstream_auth_header=_as_str(values, "upstream_auth_header", "Authorization"),
        upstream_auth_scheme=_as_str(values, "upstream_auth_scheme", "Bearer "),
        upstream_headers=_as_headers(values.get("upstream_headers", {})),
        data_dir=data_dir,
        database_file=_as_str(values, "database_file", "router.db"),
        timezone_offset_minutes=_as_int(values, "timezone_offset_minutes", 480),
        currency=_as_str(values, "currency", "USD"),
        currency_symbol=_as_str(values, "currency_symbol", "$"),
        default_price_in_micros_per_mtok=_as_price(values, "default_price_in_micros_per_mtok", 0),
        default_price_out_micros_per_mtok=_as_price(values, "default_price_out_micros_per_mtok", 0),
        default_max_output_tokens=_as_int(values, "default_max_output_tokens", 4096),
        max_request_bytes=_as_int(values, "max_request_bytes", 16 * 1024 * 1024),
        max_response_bytes=_as_int(values, "max_response_bytes", 64 * 1024 * 1024),
        max_concurrency=_as_int(values, "max_concurrency", 128),
        connect_timeout_seconds=_as_float(values, "connect_timeout_seconds", 10.0),
        request_timeout_seconds=_as_float(values, "request_timeout_seconds", 300.0),
        upstream_pool_size=_as_int(values, "upstream_pool_size", 32),
        upstream_pool_idle_seconds=_as_float(values, "upstream_pool_idle_seconds", 30.0),
        stream_chunk_size=_as_int(values, "stream_chunk_size", 64 * 1024),
        inject_stream_usage=_as_bool(values, "inject_stream_usage", True),
        verify_upstream_tls=_as_bool(values, "verify_upstream_tls", True),
        upstream_ca_file=ca_file,
        log_level=_as_str(values, "log_level", "INFO").upper(),
        users=_parse_users(values.get("users", [])),
        config_path=config_path if config_path.exists() else None,
        base_dir=base_dir,
        config_format=config_format,
    )
    _validate(config)
    return config


def _config_format(path: Path) -> str:
    return "toml" if path.suffix.lower() == ".toml" else "json"


def _read_config_document(path: Path, config_format: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"无法读取配置文件 {path}：{exc}") from exc
    if config_format == "toml":
        try:
            import tomllib
        except ModuleNotFoundError as exc:
            raise ConfigError("读取 TOML 需要 Python 3.11+（tomllib）；请改用 JSON 配置") from exc
        try:
            parsed = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"无法解析 TOML 配置文件 {path}：{exc}") from exc
    else:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"无法解析 JSON 配置文件 {path}：{exc}") from exc
    if not isinstance(parsed, dict):
        raise ConfigError("配置文件顶层必须是对象（JSON object 或 TOML table）")
    return parsed


def _parse_users(value: Any) -> tuple[ConfigUser, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise ConfigError("users 必须是数组/表数组（JSON array 或 TOML [[users]]）")
    users: list[ConfigUser] = []
    names: set[str] = set()
    keys: set[str] = set()
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ConfigError(f"users[{index}] 必须是对象")
        unknown = set(item) - _USER_FIELDS
        if unknown:
            raise ConfigError(f"users[{index}] 包含未知字段：{', '.join(sorted(unknown))}")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"users[{index}].name 必须是非空字符串")
        name = name.strip()
        if len(name) > 100 or any(char in name for char in "\r\n\x00"):
            raise ConfigError(f"users[{index}].name 非法")
        if name in names:
            raise ConfigError(f"users 中用户名重复：{name}")
        names.add(name)

        api_key = item.get("api_key")
        legacy_key = item.get("key")
        if api_key is not None and legacy_key is not None and api_key != legacy_key:
            raise ConfigError(f"users[{index}] 同时设置了 api_key 和 key，且值不同")
        if api_key is None:
            api_key = legacy_key
        if not isinstance(api_key, str):
            raise ConfigError(f"users[{index}].api_key 必须是字符串")
        api_key = api_key.strip()
        if not 16 <= len(api_key) <= 1024:
            raise ConfigError(f"users[{index}].api_key 长度必须在 16-1024 之间")
        if any(char in api_key for char in "\r\n\t \x00"):
            raise ConfigError(f"users[{index}].api_key 不能包含空白或控制字符")
        if api_key in keys:
            raise ConfigError(f"users 中存在重复的 api_key（用户 {name}）")
        keys.add(api_key)

        daily_tokens = _as_user_token_limit(item, "daily_token_limit", index)
        monthly_tokens = _as_user_token_limit(item, "monthly_token_limit", index)
        daily_cost = _as_user_cost_limit(item, "daily_cost_limit", index)
        monthly_cost = _as_user_cost_limit(item, "monthly_cost_limit", index)
        allowed = _parse_allowed_models(item.get("allowed_models", _MISSING), index)
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ConfigError(f"users[{index}].enabled 必须是 true/false")
        note = item.get("note", "")
        if not isinstance(note, str):
            raise ConfigError(f"users[{index}].note 必须是字符串")
        if len(note) > 500 or "\x00" in note:
            raise ConfigError(f"users[{index}].note 非法")

        users.append(
            ConfigUser(
                name=name,
                api_key=api_key,
                daily_token_limit=daily_tokens,
                monthly_token_limit=monthly_tokens,
                daily_cost_limit_micros=daily_cost,
                monthly_cost_limit_micros=monthly_cost,
                allowed_models=allowed,
                enabled=enabled,
                note=note.strip(),
            )
        )
    return tuple(users)


def _as_user_token_limit(item: Mapping[str, Any], field_name: str, index: int) -> int | None:
    if field_name not in item:
        return None
    value = item[field_name]
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"users[{index}].{field_name} 必须是非负整数、字符串或 null")
    if isinstance(value, int):
        if value < 0:
            raise ConfigError(f"users[{index}].{field_name} 不能为负数")
        return value
    if isinstance(value, str):
        try:
            return parse_token_quantity(value)
        except UserError as exc:
            raise ConfigError(f"users[{index}].{field_name}：{exc}") from exc
    raise ConfigError(f"users[{index}].{field_name} 必须是非负整数、字符串或 null")


def _as_user_cost_limit(item: Mapping[str, Any], field_name: str, index: int) -> int | None:
    if field_name not in item:
        return None
    value = item[field_name]
    if value is None:
        return None
    if isinstance(value, bool):
        raise ConfigError(f"users[{index}].{field_name} 必须是非负金额或 null")
    if isinstance(value, (int, float, str)):
        try:
            return parse_money_to_micros(str(value))
        except UserError as exc:
            raise ConfigError(f"users[{index}].{field_name}：{exc}") from exc
    raise ConfigError(f"users[{index}].{field_name} 必须是非负金额或 null")


def _parse_allowed_models(value: Any, index: int) -> tuple[str, ...] | None:
    if value is _MISSING or value is None:
        return None
    if not isinstance(value, list):
        raise ConfigError(f"users[{index}].allowed_models 必须是字符串数组")
    patterns: list[str] = []
    for pattern in value:
        if not isinstance(pattern, str):
            raise ConfigError(f"users[{index}].allowed_models 只能包含字符串")
        clean = pattern.strip()
        if not clean:
            raise ConfigError(f"users[{index}].allowed_models 不能包含空字符串")
        if len(clean) > 200 or any(char in clean for char in "\r\n\x00"):
            raise ConfigError(f"users[{index}].allowed_models 中的模式非法")
        if clean not in patterns:
            patterns.append(clean)
    if "*" in patterns:
        return ("*",)
    return tuple(patterns)


def ensure_default_config(path: str | Path | None = None) -> tuple[Path, bool]:
    """Create the default JSON/TOML config on first serve if it does not exist."""

    target = resolve_config_path(path)
    if target.exists():
        return target, False
    write_default_config(target)
    return target, True


def _apply_environment(values: dict[str, Any]) -> None:
    mappings: dict[str, tuple[str, type]] = {
        "TR_HOST": ("host", str),
        "TR_PORT": ("port", int),
        "TR_DATA_DIR": ("data_dir", str),
        "TR_UPSTREAM_BASE_URL": ("upstream_base_url", str),
        "TR_TIMEZONE_OFFSET_MINUTES": ("timezone_offset_minutes", int),
        "TR_LOG_LEVEL": ("log_level", str),
    }
    for env_name, (field_name, converter) in mappings.items():
        raw = os.environ.get(env_name)
        if raw is None or raw == "":
            continue
        try:
            values[field_name] = converter(raw)
        except ValueError as exc:
            raise ConfigError(f"环境变量 {env_name} 的值无效：{raw!r}") from exc


def _resolve_path(value: object, base_dir: Path, *, field_name: str) -> Path:
    if not isinstance(value, (str, os.PathLike)) or not str(value).strip():
        raise ConfigError(f"{field_name} 必须是非空路径")
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = base_dir / path
    return path.resolve()


def _as_str(values: Mapping[str, Any], key: str, default: str) -> str:
    value = values.get(key, default)
    if not isinstance(value, str):
        raise ConfigError(f"{key} 必须是字符串")
    return value


def _as_optional_str(values: Mapping[str, Any], key: str) -> str | None:
    value = values.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{key} 必须是字符串")
    return value


def _as_int(values: Mapping[str, Any], key: str, default: int) -> int:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是整数")
    return value


def _as_float(values: Mapping[str, Any], key: str, default: float) -> float:
    value = values.get(key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} 必须是数字")
    return float(value)


def _as_bool(values: Mapping[str, Any], key: str, default: bool) -> bool:
    value = values.get(key, default)
    if not isinstance(value, bool):
        raise ConfigError(f"{key} 必须是 true 或 false")
    return value


def _as_price(values: Mapping[str, Any], key: str, default: int) -> int:
    value = values.get(key, default)
    if isinstance(value, str):
        return parse_price_to_micros_per_mtok(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} 必须是非负整数（微单位/百万 token）")
    return value


def _as_headers(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError("upstream_headers 必须是字符串到字符串的对象")
    headers: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str):
            raise ConfigError("upstream_headers 的键和值都必须是字符串")
        if any(char in key for char in "\r\n:") or any(char in item for char in "\r\n"):
            raise ConfigError("upstream_headers 包含非法的换行或冒号")
        try:
            key.encode("latin-1")
            item.encode("latin-1")
        except UnicodeEncodeError as exc:
            raise ConfigError("upstream_headers 的键和值必须是 HTTP 头可编码的字符") from exc
        if key.lower() in _FORBIDDEN_UPSTREAM_HEADERS:
            raise ConfigError(f"upstream_headers 不允许设置 {key}（由路由器管理）")
        headers[key] = item
    return headers


def _validate(config: Config) -> None:
    if config.config_format not in {"json", "toml"}:
        raise ConfigError("config_format 必须是 json 或 toml")
    if not config.host.strip():
        raise ConfigError("host 不能为空")
    if not 1 <= config.port <= 65535:
        raise ConfigError("port 必须在 1-65535 之间")
    parsed = urlparse(config.upstream_base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ConfigError("upstream_base_url 必须是 http(s) URL 且包含主机名")
    if parsed.username or parsed.password:
        raise ConfigError("upstream_base_url 不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ConfigError("upstream_base_url 不能包含查询串或锚点")
    try:
        parsed.port
    except ValueError as exc:
        raise ConfigError("upstream_base_url 的端口无效") from exc
    if not config.upstream_api_key_env or any(c in config.upstream_api_key_env for c in "=\x00"):
        raise ConfigError("upstream_api_key_env 不是合法环境变量名")
    if not config.upstream_auth_header or any(c in config.upstream_auth_header for c in "\r\n:"):
        raise ConfigError("upstream_auth_header 非法")
    if any(c in config.upstream_auth_scheme for c in "\r\n"):
        raise ConfigError("upstream_auth_scheme 非法")
    if config.database_file != Path(config.database_file).name or Path(config.database_file).is_absolute():
        raise ConfigError("database_file 必须是 data_dir 下的单个文件名")
    if not -720 <= config.timezone_offset_minutes <= 840:
        raise ConfigError("timezone_offset_minutes 必须在 -720 到 840 之间")
    if not config.currency or len(config.currency) > 8:
        raise ConfigError("currency 必须是 1-8 个字符")
    if not config.currency_symbol or len(config.currency_symbol) > 8:
        raise ConfigError("currency_symbol 必须是 1-8 个字符")
    if config.default_price_in_micros_per_mtok < 0 or config.default_price_out_micros_per_mtok < 0:
        raise ConfigError("默认价格不能为负数")
    if not 1 <= config.default_max_output_tokens <= 1_000_000:
        raise ConfigError("default_max_output_tokens 必须在 1-1000000 之间")
    if config.max_request_bytes < 1024:
        raise ConfigError("max_request_bytes 不能小于 1024")
    if config.max_response_bytes < 1024:
        raise ConfigError("max_response_bytes 不能小于 1024")
    if not 1 <= config.max_concurrency <= 4096:
        raise ConfigError("max_concurrency 必须在 1-4096 之间")
    if config.connect_timeout_seconds <= 0 or config.request_timeout_seconds <= 0:
        raise ConfigError("超时时间必须大于 0")
    if not 0 <= config.upstream_pool_size <= 1024:
        raise ConfigError("upstream_pool_size 必须在 0-1024 之间")
    if config.upstream_pool_idle_seconds <= 0:
        raise ConfigError("upstream_pool_idle_seconds 必须大于 0")
    if not 1024 <= config.stream_chunk_size <= 4 * 1024 * 1024:
        raise ConfigError("stream_chunk_size 必须在 1 KiB-4 MiB 之间")
    if config.log_level not in _LOG_LEVELS:
        raise ConfigError(f"log_level 必须是 {', '.join(sorted(_LOG_LEVELS))} 之一")
    if config.upstream_ca_file is not None and not config.upstream_ca_file.is_file():
        raise ConfigError(f"upstream_ca_file 不存在：{config.upstream_ca_file}")


DEFAULT_CONFIG_DOCUMENT: dict[str, Any] = {
    "host": "127.0.0.1",
    "port": 8787,
    "upstream_base_url": "https://api.openai.com/v1",
    "upstream_api_key": "",
    "upstream_api_key_env": "TOKEN_ROUTER_UPSTREAM_KEY",
    "upstream_auth_header": "Authorization",
    "upstream_auth_scheme": "Bearer ",
    "upstream_headers": {},
    "data_dir": "data",
    "database_file": "router.db",
    "timezone_offset_minutes": 480,
    "currency": "USD",
    "currency_symbol": "$",
    "default_price_in_micros_per_mtok": 0,
    "default_price_out_micros_per_mtok": 0,
    "default_max_output_tokens": 4096,
    "max_request_bytes": 16777216,
    "max_response_bytes": 67108864,
    "max_concurrency": 128,
    "connect_timeout_seconds": 10.0,
    "request_timeout_seconds": 300.0,
    "upstream_pool_size": 32,
    "upstream_pool_idle_seconds": 30.0,
    "stream_chunk_size": 65536,
    "inject_stream_usage": True,
    "verify_upstream_tls": True,
    "upstream_ca_file": None,
    "log_level": "INFO",
    "users": [],
}


def write_default_config(
    path: str | Path,
    *,
    force: bool = False,
    updates: dict[str, Any] | None = None,
    format: str | None = None,
) -> Path:
    target = Path(path).expanduser()
    if not target.is_absolute():
        target = Path.cwd() / target
    if target.exists() and not force:
        raise ConfigError(f"配置文件已存在：{target}（如需覆盖请使用 --force）")
    config_format = (format or _config_format(target)).lower()
    if config_format not in {"json", "toml"}:
        raise ConfigError("配置格式只能是 json 或 toml")
    if config_format == "toml" and target.suffix.lower() != ".toml":
        raise ConfigError("TOML 配置必须使用 .toml 后缀")
    if config_format == "json" and target.suffix.lower() == ".toml":
        raise ConfigError("JSON 配置不能使用 .toml 后缀")
    target.parent.mkdir(parents=True, exist_ok=True)
    document = dict(DEFAULT_CONFIG_DOCUMENT)
    if updates:
        unknown = set(updates) - set(document)
        if unknown:
            raise ConfigError(f"未知配置项：{', '.join(sorted(unknown))}")
        document.update(updates)
    if config_format == "toml":
        text = _toml_dumps(document)
    else:
        text = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    target.write_text(text, encoding="utf-8")
    return target


def _toml_dumps(document: Mapping[str, Any]) -> str:
    lines = ["# TokenRouter configuration. JSON is also supported.", ""]
    tables: list[tuple[str, Any]] = []
    for key, value in document.items():
        if value is None:
            continue
        if isinstance(value, dict):
            if value:
                tables.append((key, value))
            else:
                lines.append(f"{key} = {{}}")
            continue
        if isinstance(value, list) and value and all(isinstance(item, dict) for item in value):
            tables.append((key, value))
            continue
        lines.append(f"{key} = {_toml_value(value)}")
    for key, value in tables:
        if isinstance(value, list):
            for item in value:
                lines.append("")
                lines.append(f"[[{key}]]")
                lines.extend(_toml_table_lines(item))
        else:
            lines.append("")
            lines.append(f"[{key}]")
            lines.extend(_toml_table_lines(value))
    return "\n".join(lines).rstrip() + "\n"


def _toml_table_lines(mapping: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, dict):
            if value:
                raise ConfigError("TOML 写入不支持嵌套表")
            lines.append(f"{key} = {{}}")
        else:
            lines.append(f"{key} = {_toml_value(value)}")
    return lines


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        if not value:
            return "{}"
        return "{ " + ", ".join(
            f"{key} = {_toml_value(item)}" for key, item in value.items()
        ) + " }"
    raise ConfigError(f"无法写入 TOML 值：{value!r}")
