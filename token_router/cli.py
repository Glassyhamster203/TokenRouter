"""Command-line administration for TokenRouter (no extra admin port)."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .config import (
    CONFIG_ENV,
    Config,
    ConfigError,
    ensure_default_config,
    load_config,
    resolve_config_path,
    write_default_config,
)
from .db import ApiKey, Database, DatabaseError, KeyUsageOverview, UsageTotals
from .proxy import run_server
from .util import (
    UserError,
    format_amount,
    format_int,
    format_price_per_mtok,
    format_table,
    format_timestamp,
    generate_api_key,
    parse_optional_money,
    parse_price_to_micros_per_mtok,
    parse_token_quantity,
    period_bounds,
)

_UNSET = object()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="token-router",
        description="单端口、零第三方依赖的 OpenAI 兼容 token 路由与计费网关",
    )
    parser.add_argument("-c", "--config", help="配置文件路径（默认 ./config.json 或 TR_CONFIG）")
    parser.add_argument("--version", action="version", version=f"TokenRouter {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="命令")

    init_parser = subparsers.add_parser("init", help="创建配置和 SQLite 数据库")
    init_parser.add_argument("--force", action="store_true", help="覆盖已有配置文件")
    init_parser.add_argument("--upstream-base-url", help="上游 OpenAI 兼容 API 基础地址")
    init_parser.add_argument("--format", choices=["json", "toml"], help="配置格式（默认按后缀；.toml 为 TOML）")
    init_parser.set_defaults(func=cmd_init)

    serve_parser = subparsers.add_parser("serve", help="启动网关（仅此一个端口）")
    serve_parser.add_argument("--host", help="覆盖监听地址（默认 127.0.0.1）")
    serve_parser.add_argument("--port", type=int, help="覆盖监听端口")
    serve_parser.add_argument("--log-level", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    serve_parser.set_defaults(func=cmd_serve)

    status_parser = subparsers.add_parser("status", help="查看运行配置与总体用量")
    status_parser.add_argument("--json", action="store_true", help="输出 JSON")
    status_parser.set_defaults(func=cmd_status)

    key_parser = subparsers.add_parser("key", help="管理下游虚拟 key")
    key_sub = key_parser.add_subparsers(dest="key_command", required=True, metavar="操作")

    create_parser = key_sub.add_parser("create", help="创建 key（明文只显示一次）")
    create_parser.add_argument("--name", required=True, help="便于识别的名称")
    create_parser.add_argument("--daily-tokens", help="每日 token 限额（100k/1.5m/none）")
    create_parser.add_argument("--monthly-tokens", help="每月 token 限额（100k/1.5m/none）")
    create_parser.add_argument("--daily-cost", help="每日费用限额，单位同配置 currency（如 2.50）")
    create_parser.add_argument("--monthly-cost", help="每月费用限额，单位同配置 currency")
    create_parser.add_argument("--price-in", help="输入价格 / 百万 token（覆盖默认）")
    create_parser.add_argument("--price-out", help="输出价格 / 百万 token（覆盖默认）")
    create_parser.add_argument(
        "--allowed-model",
        action="append",
        default=None,
        help="允许的模型/通配模式，可重复；不填表示全部允许",
    )
    create_parser.add_argument("--note", default="", help="备注")
    create_parser.set_defaults(func=cmd_key_create)

    list_parser = key_sub.add_parser("list", help="列出 key")
    list_parser.add_argument("--all", action="store_true", help="包含已吊销的 key")
    list_parser.set_defaults(func=cmd_key_list)

    show_parser = key_sub.add_parser("show", help="查看单个 key 的配置和当前用量")
    show_parser.add_argument("reference", help="key ID 或前缀")
    show_parser.set_defaults(func=cmd_key_show)

    update_parser = key_sub.add_parser("update", help="修改价格、限额、名称或备注")
    update_parser.add_argument("reference", help="key ID 或前缀")
    update_parser.add_argument("--name", default=argparse.SUPPRESS)
    update_parser.add_argument("--daily-tokens", default=argparse.SUPPRESS)
    update_parser.add_argument("--monthly-tokens", default=argparse.SUPPRESS)
    update_parser.add_argument("--daily-cost", default=argparse.SUPPRESS)
    update_parser.add_argument("--monthly-cost", default=argparse.SUPPRESS)
    update_parser.add_argument("--price-in", default=argparse.SUPPRESS)
    update_parser.add_argument("--price-out", default=argparse.SUPPRESS)
    update_parser.add_argument("--note", default=argparse.SUPPRESS)
    update_parser.set_defaults(func=cmd_key_update)

    for name, help_text, function in (
        ("enable", "启用 key", cmd_key_enable),
        ("disable", "停用 key（保留数据）", cmd_key_disable),
    ):
        action_parser = key_sub.add_parser(name, help=help_text)
        action_parser.add_argument("reference", help="key ID 或前缀")
        action_parser.set_defaults(func=function)

    revoke_parser = key_sub.add_parser("revoke", help="永久吊销 key")
    revoke_parser.add_argument("reference", help="key ID 或前缀")
    revoke_parser.add_argument("--yes", action="store_true", help="跳过确认")
    revoke_parser.set_defaults(func=cmd_key_revoke)

    rotate_parser = key_sub.add_parser("rotate", help="生成新 key 并立即作废旧 key")
    rotate_parser.add_argument("reference", help="key ID 或前缀")
    rotate_parser.add_argument("--yes", action="store_true", help="跳过确认")
    rotate_parser.set_defaults(func=cmd_key_rotate)

    models_parser = key_sub.add_parser("models", help="查看或设置 key 可用的模型")
    models_sub = models_parser.add_subparsers(dest="models_command", required=True, metavar="操作")
    models_show = models_sub.add_parser("show", help="查看模型权限")
    models_show.add_argument("reference", help="key ID 或前缀")
    models_show.set_defaults(func=cmd_key_models_show)
    models_set = models_sub.add_parser("set", help="设置模型权限（精确名称或通配模式）")
    models_set.add_argument("reference", help="key ID 或前缀")
    models_set.add_argument("--model", action="append", default=None, help="允许的模型/模式，可重复")
    models_set.add_argument("--all", action="store_true", help="取消限制，允许全部模型")
    models_set.add_argument("--none", action="store_true", help="禁止所有模型")
    models_set.set_defaults(func=cmd_key_models_set)

    usage_parser = subparsers.add_parser("usage", help="按日/月查看各 key 用量与限额")
    usage_parser.add_argument("--key", help="只显示指定 key（ID 或前缀）")
    usage_parser.add_argument("--all", action="store_true", help="包含已吊销的 key")
    usage_parser.add_argument("--json", action="store_true", help="输出 JSON")
    usage_parser.set_defaults(func=cmd_usage)

    price_parser = subparsers.add_parser("price", help="管理按模型的输入/输出/缓存价格")
    price_sub = price_parser.add_subparsers(dest="price_command", required=True, metavar="操作")

    price_set = price_sub.add_parser("set", help="设置全局或指定 key 的模型价格")
    price_set.add_argument("--model", required=True, help="上游模型 ID")
    price_set.add_argument("--input", required=True, help="输入价格 / 百万 token")
    price_set.add_argument("--output", required=True, help="输出价格 / 百万 token")
    price_set.add_argument("--cached", help="缓存命中输入价格 / 百万 token；省略则同输入价")
    price_set.add_argument("--key", help="只对指定 key 生效（ID 或前缀）；省略为全局价")
    price_set.set_defaults(func=cmd_price_set)

    price_list = price_sub.add_parser("list", help="列出模型价格")
    price_list.add_argument("--key", help="同时列出该 key 的覆盖价")
    price_list.set_defaults(func=cmd_price_list)

    price_remove = price_sub.add_parser("remove", help="删除模型价格")
    price_remove.add_argument("--model", required=True, help="模型 ID")
    price_remove.add_argument("--key", help="删除指定 key 的覆盖价；省略为全局价")
    price_remove.add_argument("--yes", action="store_true", help="跳过确认")
    price_remove.set_defaults(func=cmd_price_remove)

    requests_parser = subparsers.add_parser("requests", help="查看请求级账本")
    requests_parser.add_argument("--key", help="只显示指定 key（ID 或前缀）")
    requests_parser.add_argument("--limit", type=int, default=30, help="条数（默认 30，最大 1000）")
    requests_parser.add_argument("--json", action="store_true", help="输出 JSON")
    requests_parser.set_defaults(func=cmd_requests)

    config_parser = subparsers.add_parser("show-config", help="显示生效配置（不显示上游 key）")
    config_parser.set_defaults(func=cmd_show_config)

    prune_parser = subparsers.add_parser("prune", help="清理旧的请求级账本（保留计费汇总）")
    prune_parser.add_argument("--days", type=int, default=90, help="保留最近多少天（默认 90）")
    prune_parser.add_argument("--yes", action="store_true", help="跳过确认")
    prune_parser.set_defaults(func=cmd_prune)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.ERROR,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        return 130
    except (UserError, ConfigError, DatabaseError, sqlite3.Error, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2


# --------------------------------------------------------------------------- init
def cmd_init(args: argparse.Namespace) -> int:
    if args.format == "toml" and not args.config and not os.environ.get(CONFIG_ENV):
        path = (Path.cwd() / "config.toml").resolve()
    else:
        path = resolve_config_path(args.config)
    updates: dict[str, Any] = {}
    if args.upstream_base_url:
        updates["upstream_base_url"] = args.upstream_base_url
    if path.exists() and not args.force:
        if updates:
            raise ConfigError(
                "配置文件已存在，--upstream-base-url 未生效；请手动修改配置或加 --force 覆盖"
            )
        print(f"配置已存在，保留现有文件：{path}")
    else:
        written = write_default_config(path, force=args.force, updates=updates, format=args.format)
        print(f"已写入配置：{written}")
    config = load_config(path, require_exists=True)
    db = Database(config.database_path, config.timezone_offset_minutes)
    db.initialize()
    print(f"数据库已就绪：{config.database_path}")
    print()
    print("下一步：")
    if os.name == "nt":
        print('  $env:TOKEN_ROUTER_UPSTREAM_KEY = "sk-..."   # 你的唯一上游 key')
        print('  .\\run.cmd key create --name "my-app"')
        print("  .\\run.cmd serve")
    else:
        print('  export TOKEN_ROUTER_UPSTREAM_KEY="sk-..."   # 你的唯一上游 key')
        print('  python3 -m token_router key create --name "my-app"')
        print("  python3 -m token_router serve")
    print()
    print(f"下游统一接入地址：{_client_base_url(config)}/v1")
    return 0


# -------------------------------------------------------------------------- serve
def cmd_serve(args: argparse.Namespace) -> int:
    config_path = resolve_config_path(args.config)
    if not config_path.exists():
        generated, _ = ensure_default_config(args.config)
        print(
            f"首次启动：已生成默认配置文件 {generated}\n"
            "请在其中填写 upstream_api_key（或设置环境变量），并按需添加 users；"
            "修改后重新启动即可。",
            flush=True,
        )
    config = _load_config(args)
    if args.host:
        config = config.with_overrides(host=args.host)
    if args.port is not None:
        config = config.with_overrides(port=args.port)
    if args.log_level:
        config = config.with_overrides(log_level=args.log_level)
    _validate_runtime_overrides(config)
    logging.basicConfig(
        level=getattr(logging, config.log_level),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        force=True,
    )
    run_server(config)
    return 0


def _validate_runtime_overrides(config: Config) -> None:
    if not 1 <= config.port <= 65535:
        raise UserError("端口必须在 1-65535 之间")
    if not config.host.strip():
        raise UserError("监听地址不能为空")


# ------------------------------------------------------------------------- status
def cmd_status(args: argparse.Namespace) -> int:
    config = _load_config(args)
    db = Database(config.database_path, config.timezone_offset_minutes)
    db.initialize()
    now = int(time.time())
    bounds = period_bounds(now, config.timezone_offset_minutes)
    overview = db.usage_overview(bounds, include_revoked=True)
    totals_day = _sum_totals(item.day for item in overview)
    totals_month = _sum_totals(item.month for item in overview)
    counts = db.counts()
    if args.json:
        print(
            json.dumps(
                {
                    **config.public_view(),
                    "counts": counts,
                    "day": _totals_json(totals_day),
                    "month": _totals_json(totals_month),
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    print(f"配置：      {config.config_path or '(内置默认值)'}")
    print(f"监听地址：  {_client_base_url(config)}")
    print(f"上游地址：  {config.base_url_description}")
    print(f"上游 key：  {'已设置' if config.upstream_api_key else '未在当前管理进程环境中设置（serve 会单独校验）'}")
    print(f"数据库：    {config.database_path}")
    print(f"计费时区：  UTC{_offset_text(config.timezone_offset_minutes)}")
    print(f"计费货币：  {config.currency}（{config.currency_symbol}）")
    print(f"Key 数量：  {counts['active_keys']} 活跃 / {counts['total_keys']} 总计")
    print(f"请求记录：  {counts['requests']:,}")
    print()
    print(f"今日（{format_timestamp(bounds.day_start, config.timezone_offset_minutes)} 起）")
    print(f"  tokens={format_int(totals_day.total_tokens)}  "
          f"缓存命中={format_int(totals_day.cached_input_tokens)}  "
          f"费用={format_amount(totals_day.cost_micros, config.currency_symbol)}  "
          f"请求={totals_day.requests}")
    print(f"本月（{format_timestamp(bounds.month_start, config.timezone_offset_minutes)} 起）")
    print(f"  tokens={format_int(totals_month.total_tokens)}  "
          f"缓存命中={format_int(totals_month.cached_input_tokens)}  "
          f"费用={format_amount(totals_month.cost_micros, config.currency_symbol)}  "
          f"请求={totals_month.requests}")
    return 0


# ---------------------------------------------------------------------------- key
def cmd_key_create(args: argparse.Namespace) -> int:
    config, db = _config_and_db(args)
    price_in = (
        parse_price_to_micros_per_mtok(args.price_in)
        if args.price_in is not None
        else config.default_price_in_micros_per_mtok
    )
    price_out = (
        parse_price_to_micros_per_mtok(args.price_out)
        if args.price_out is not None
        else config.default_price_out_micros_per_mtok
    )
    daily_tokens = parse_token_quantity(args.daily_tokens) if args.daily_tokens is not None else None
    monthly_tokens = parse_token_quantity(args.monthly_tokens) if args.monthly_tokens is not None else None
    daily_cost = parse_optional_money(args.daily_cost) if args.daily_cost is not None else None
    monthly_cost = parse_optional_money(args.monthly_cost) if args.monthly_cost is not None else None
    raw_key = generate_api_key()
    key = db.create_key(
        name=args.name,
        api_key=raw_key,
        price_in_micros_per_mtok=price_in,
        price_out_micros_per_mtok=price_out,
        daily_token_limit=daily_tokens,
        monthly_token_limit=monthly_tokens,
        daily_cost_limit_micros=daily_cost,
        monthly_cost_limit_micros=monthly_cost,
        note=args.note,
        allowed_models=tuple(args.allowed_model) if args.allowed_model else None,
    )
    print(f"已创建 key #{key.id}（{key.name}）")
    print()
    print("API key（只显示这一次，请立即保存）：")
    print(f"  {raw_key}")
    print()
    print(f"前缀：      {key.key_prefix}")
    print(f"输入价格：  {format_price_per_mtok(key.price_in_micros_per_mtok, config.currency_symbol)}")
    print(f"输出价格：  {format_price_per_mtok(key.price_out_micros_per_mtok, config.currency_symbol)}")
    print("缓存命中：  默认按输入价；可用 price set 为具体模型设置")
    print(f"模型权限：  {_models_text(key.allowed_models)}")
    print(f"每日限额：  {_limit_text(key.daily_token_limit)} tokens / {_cost_limit_text(key.daily_cost_limit_micros, config)}")
    print(f"每月限额：  {_limit_text(key.monthly_token_limit)} tokens / {_cost_limit_text(key.monthly_cost_limit_micros, config)}")
    print()
    print(f"下游接入：  {_client_base_url(config)}/v1")
    print("请求头：    Authorization: Bearer <上面显示的 API key>")
    return 0


def cmd_key_list(args: argparse.Namespace) -> int:
    config, db = _config_and_db(args)
    keys = db.list_keys(include_revoked=args.all)
    if not keys:
        print("还没有创建 key。使用 key create 创建。")
        return 0
    rows = []
    for key in keys:
        rows.append(
            [
                key.id,
                key.name,
                key.key_prefix,
                _key_state(key),
                format_price_per_mtok(key.price_in_micros_per_mtok, config.currency_symbol),
                format_price_per_mtok(key.price_out_micros_per_mtok, config.currency_symbol),
                _limit_text(key.daily_token_limit),
                _limit_text(key.monthly_token_limit),
                _cost_limit_text(key.daily_cost_limit_micros, config),
                _cost_limit_text(key.monthly_cost_limit_micros, config),
                _models_text(key.allowed_models),
            ]
        )
    print(
        format_table(
            ["ID", "名称", "前缀", "状态", "输入价", "输出价", "日token", "月token", "日费用", "月费用", "模型权限"],
            rows,
            numeric=[0],
        )
    )
    return 0


def cmd_key_show(args: argparse.Namespace) -> int:
    config, db = _config_and_db(args)
    key = db.resolve_key(args.reference)
    bounds = period_bounds(int(time.time()), config.timezone_offset_minutes)
    day, month = db.get_period_usage(key.id, bounds)
    print(f"Key #{key.id}  {key.name}")
    print(f"  前缀：      {key.key_prefix}")
    print(f"  状态：      {_key_state(key)}")
    print(f"  创建时间：  {format_timestamp(key.created_at, config.timezone_offset_minutes)}")
    print(f"  最后使用：  {format_timestamp(key.last_used_at, config.timezone_offset_minutes)}")
    print(f"  输入价格：  {format_price_per_mtok(key.price_in_micros_per_mtok, config.currency_symbol)}")
    print(f"  输出价格：  {format_price_per_mtok(key.price_out_micros_per_mtok, config.currency_symbol)}")
    print("  缓存命中：  默认按输入价")
    overrides = db.list_model_prices(key_id=key.id, include_global=False)
    print(f"  模型覆盖：  {len(overrides)} 条（price list --key {key.id}）")
    print(f"  模型权限：  {_models_text(key.allowed_models)}")
    print(f"  备注：      {key.note or '-'}")
    print()
    _print_period_detail("今日", day, key.daily_token_limit, key.daily_cost_limit_micros, config, bounds.next_day)
    _print_period_detail("本月", month, key.monthly_token_limit, key.monthly_cost_limit_micros, config, bounds.next_month)
    return 0


def cmd_key_update(args: argparse.Namespace) -> int:
    config, db = _config_and_db(args)
    key = db.resolve_key(args.reference)
    changes: dict[str, Any] = {}
    for attr, field_name in (
        ("name", "name"),
        ("note", "note"),
    ):
        value = getattr(args, attr, _UNSET)
        if value is not _UNSET:
            changes[field_name] = value
    for attr, field_name in (
        ("price_in", "price_in_micros_per_mtok"),
        ("price_out", "price_out_micros_per_mtok"),
    ):
        value = getattr(args, attr, _UNSET)
        if value is not _UNSET:
            changes[field_name] = parse_price_to_micros_per_mtok(value)
    for attr, field_name in (
        ("daily_tokens", "daily_token_limit"),
        ("monthly_tokens", "monthly_token_limit"),
    ):
        value = getattr(args, attr, _UNSET)
        if value is not _UNSET:
            changes[field_name] = parse_token_quantity(value)
    for attr, field_name in (
        ("daily_cost", "daily_cost_limit_micros"),
        ("monthly_cost", "monthly_cost_limit_micros"),
    ):
        value = getattr(args, attr, _UNSET)
        if value is not _UNSET:
            changes[field_name] = parse_optional_money(value)
    if not changes:
        raise UserError("没有提供要修改的字段")
    updated = db.update_key(key.id, **changes)
    print(f"已更新 key #{updated.id}（{updated.name}）")
    print(f"  输入价：{format_price_per_mtok(updated.price_in_micros_per_mtok, config.currency_symbol)}  "
          f"输出价：{format_price_per_mtok(updated.price_out_micros_per_mtok, config.currency_symbol)}")
    print(f"  每日：{_limit_text(updated.daily_token_limit)} tokens / {_cost_limit_text(updated.daily_cost_limit_micros, config)}")
    print(f"  每月：{_limit_text(updated.monthly_token_limit)} tokens / {_cost_limit_text(updated.monthly_cost_limit_micros, config)}")
    return 0


def cmd_key_enable(args: argparse.Namespace) -> int:
    _, db = _config_and_db(args)
    key = db.resolve_key(args.reference)
    if key.revoked_at is not None:
        raise UserError("该 key 已永久吊销，不能重新启用；请使用 key rotate 生成新 key")
    updated = db.set_enabled(key.id, True)
    print(f"已启用 key #{updated.id}（{updated.name}）")
    return 0


def cmd_key_disable(args: argparse.Namespace) -> int:
    _, db = _config_and_db(args)
    key = db.resolve_key(args.reference)
    updated = db.set_enabled(key.id, False)
    print(f"已停用 key #{updated.id}（{updated.name}）")
    return 0


def cmd_key_revoke(args: argparse.Namespace) -> int:
    _, db = _config_and_db(args)
    key = db.resolve_key(args.reference)
    _confirm(args.yes, f"永久吊销 key #{key.id}（{key.name}）？该操作不可撤销 [y/N] ")
    updated = db.revoke_key(key.id)
    print(f"已吊销 key #{updated.id}（{updated.name}）")
    return 0


def cmd_key_rotate(args: argparse.Namespace) -> int:
    _, db = _config_and_db(args)
    key = db.resolve_key(args.reference)
    _confirm(args.yes, f"轮换 key #{key.id}（{key.name}）？旧 key 将立即失效 [y/N] ")
    raw_key = generate_api_key()
    updated = db.rotate_key(key.id, raw_key)
    print(f"已轮换 key #{updated.id}（{updated.name}）")
    print("新的 API key（只显示这一次）：")
    print(f"  {raw_key}")
    print(f"旧前缀 {key.key_prefix} 已失效。")
    return 0


def cmd_key_models_show(args: argparse.Namespace) -> int:
    _, db = _config_and_db(args)
    key = db.resolve_key(args.reference)
    print(f"Key #{key.id}（{key.name}）模型权限：{_models_text(key.allowed_models)}")
    if key.allowed_models:
        for pattern in key.allowed_models:
            print(f"  - {pattern}")
    return 0


def cmd_key_models_set(args: argparse.Namespace) -> int:
    _, db = _config_and_db(args)
    key = db.resolve_key(args.reference)
    chosen = sum(bool(value) for value in (args.all, args.none, args.model))
    if chosen != 1:
        raise UserError("请指定 --all、--none 或一个/多个 --model，且只能选择一种方式")
    if args.all:
        allowed: tuple[str, ...] | None = None
        label = "全部模型"
    elif args.none:
        allowed = ()
        label = "禁止全部模型"
    else:
        allowed = tuple(args.model or ())
        label = "、".join(allowed)
    updated = db.update_key(key.id, allowed_models=allowed)
    print(f"已更新 key #{updated.id}（{updated.name}）模型权限：{label}")
    return 0


# ------------------------------------------------------------------------- price
def cmd_price_set(args: argparse.Namespace) -> int:
    config, db = _config_and_db(args)
    key = db.resolve_key(args.key) if args.key else None
    cached: int | None = None
    if args.cached is not None and args.cached.strip().lower() not in {"same", "input", "none"}:
        cached = parse_price_to_micros_per_mtok(args.cached)
    price = db.set_model_price(
        model=args.model,
        input_micros_per_mtok=parse_price_to_micros_per_mtok(args.input),
        output_micros_per_mtok=parse_price_to_micros_per_mtok(args.output),
        cached_input_micros_per_mtok=cached,
        key_id=key.id if key else None,
    )
    scope = f"key #{key.id}（{key.name}）" if key else "全局"
    print(f"已设置 {scope} 模型价格：{price.model}")
    print(f"  输入：      {format_price_per_mtok(price.input_micros_per_mtok, config.currency_symbol)}")
    print(f"  输出：      {format_price_per_mtok(price.output_micros_per_mtok, config.currency_symbol)}")
    print(
        "  缓存命中：  "
        + format_price_per_mtok(price.effective_cached_input_micros_per_mtok, config.currency_symbol)
    )
    return 0


def cmd_price_list(args: argparse.Namespace) -> int:
    config, db = _config_and_db(args)
    key = db.resolve_key(args.key) if args.key else None
    prices = db.list_model_prices(key_id=key.id if key else None, include_global=key is not None)
    if not prices:
        target = f"key #{key.id}" if key else "全局"
        print(f"{target}还没有模型价格。使用 price set 添加。")
        return 0
    rows = []
    for price in prices:
        scope = "全局" if price.key_id is None else f"key #{price.key_id}" + (f"（{key.name}）" if key else "")
        rows.append(
            [
                scope,
                price.model,
                format_price_per_mtok(price.input_micros_per_mtok, config.currency_symbol),
                format_price_per_mtok(price.output_micros_per_mtok, config.currency_symbol),
                format_price_per_mtok(price.effective_cached_input_micros_per_mtok, config.currency_symbol),
                format_timestamp(price.updated_at, config.timezone_offset_minutes),
            ]
        )
    print(
        format_table(
            ["范围", "模型", "输入价", "输出价", "缓存命中价", "更新时间"],
            rows,
        )
    )
    if key:
        print("提示：key 覆盖价优先于全局价；未匹配任何模型价时回退到 key 默认输入/输出价。")
    return 0


def cmd_price_remove(args: argparse.Namespace) -> int:
    _, db = _config_and_db(args)
    key = db.resolve_key(args.key) if args.key else None
    scope = f"key #{key.id}（{key.name}）" if key else "全局"
    _confirm(args.yes, f"删除 {scope} 模型 {args.model!r} 的价格？ [y/N] ")
    if not db.remove_model_price(args.model, key_id=key.id if key else None):
        raise UserError("没有找到对应的模型价格")
    print(f"已删除 {scope} 模型 {args.model!r} 的价格。")
    return 0


# -------------------------------------------------------------------------- usage
def cmd_usage(args: argparse.Namespace) -> int:
    config, db = _config_and_db(args)
    now = int(time.time())
    bounds = period_bounds(now, config.timezone_offset_minutes)
    overview = db.usage_overview(bounds, include_revoked=args.all)
    if args.key:
        selected = db.resolve_key(args.key)
        overview = [item for item in overview if item.key.id == selected.id]
        if not overview:
            # The key may be revoked and --all was not supplied.
            raise UserError("指定的 key 不在当前统计范围内，尝试加上 --all")
    if args.json:
        print(
            json.dumps(
                {
                    "generated_at": now,
                    "timezone_offset_minutes": config.timezone_offset_minutes,
                    "currency": config.currency,
                    "currency_symbol": config.currency_symbol,
                    "day_start": bounds.day_start,
                    "month_start": bounds.month_start,
                    "next_day": bounds.next_day,
                    "next_month": bounds.next_month,
                    "keys": [_overview_json(item, config) for item in overview],
                    "totals": {
                        "day": _totals_json(_sum_totals(item.day for item in overview)),
                        "month": _totals_json(_sum_totals(item.month for item in overview)),
                    },
                },
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0

    if not overview:
        print("还没有可统计的 key。")
        return 0
    print(f"计费时区 UTC{_offset_text(config.timezone_offset_minutes)}，金额单位 {config.currency}")
    print()
    print(f"今日（{format_timestamp(bounds.day_start, config.timezone_offset_minutes)} 起）")
    print(_usage_table(overview, "day", config))
    print()
    print(f"本月（{format_timestamp(bounds.month_start, config.timezone_offset_minutes)} 起）")
    print(_usage_table(overview, "month", config))
    day_total = _sum_totals(item.day for item in overview)
    month_total = _sum_totals(item.month for item in overview)
    print()
    print(f"合计：今日 {format_int(day_total.total_tokens)} tokens（缓存命中 "
          f"{format_int(day_total.cached_input_tokens)}） / "
          f"{format_amount(day_total.cost_micros, config.currency_symbol)}；"
          f"本月 {format_int(month_total.total_tokens)} tokens（缓存命中 "
          f"{format_int(month_total.cached_input_tokens)}） / "
          f"{format_amount(month_total.cost_micros, config.currency_symbol)}")
    print("提示：* 表示上游未返回 usage，金额和 token 为本地估算值；缓存命中是输入 token 的子集。")
    return 0


def _usage_table(overview: list[KeyUsageOverview], period: str, config: Config) -> str:
    rows = []
    for item in overview:
        totals = item.day if period == "day" else item.month
        token_limit = item.key.token_limit(period)
        cost_limit = item.key.cost_limit(period)
        rows.append(
            [
                item.key.id,
                item.key.name,
                totals.requests,
                format_int(totals.total_tokens),
                format_int(totals.cached_input_tokens),
                format_amount(totals.cost_micros, config.currency_symbol),
                _limit_text(token_limit),
                _cost_limit_text(cost_limit, config),
                _remaining_text(token_limit, totals.total_tokens),
            ]
        )
    rows.append(
        [
            "",
            "合计",
            sum(item.day.requests if period == "day" else item.month.requests for item in overview),
            format_int(sum((item.day if period == "day" else item.month).total_tokens for item in overview)),
            format_int(sum((item.day if period == "day" else item.month).cached_input_tokens for item in overview)),
            format_amount(
                sum((item.day if period == "day" else item.month).cost_micros for item in overview),
                config.currency_symbol,
            ),
            "",
            "",
            "",
        ]
    )
    return format_table(
        ["ID", "名称", "请求", "Tokens", "缓存", "费用", "Token限额", "费用限额", "剩余token"],
        rows,
        numeric=[0, 2, 3, 4, 5, 6, 7, 8],
    )


# ----------------------------------------------------------------------- requests
def cmd_requests(args: argparse.Namespace) -> int:
    config, db = _config_and_db(args)
    key_id = None
    if args.key:
        key_id = db.resolve_key(args.key).id
    entries = db.recent_requests(limit=args.limit, key_id=key_id)
    if args.json:
        print(
            json.dumps(
                [
                    {
                        "request_id": entry.request_id,
                        "key_id": entry.key_id,
                        "key_name": entry.key_name,
                        "timestamp": entry.ts,
                        "endpoint": entry.endpoint,
                        "model": entry.model,
                        "status": entry.status,
                        "input_tokens": entry.input_tokens,
                        "cached_input_tokens": entry.cached_input_tokens,
                        "output_tokens": entry.output_tokens,
                        "total_tokens": entry.total_tokens,
                        "cost_micros": entry.cost_micros,
                        "estimated": entry.estimated,
                        "latency_ms": entry.latency_ms,
                        "client_ip": entry.client_ip,
                        "error": entry.error,
                    }
                    for entry in entries
                ],
                ensure_ascii=False,
                indent=2,
            )
        )
        return 0
    if not entries:
        print("没有请求记录。")
        return 0
    rows = [
        [
            format_timestamp(entry.ts, config.timezone_offset_minutes),
            entry.key_name,
            entry.status,
            format_int(entry.input_tokens),
            format_int(entry.cached_input_tokens),
            format_int(entry.output_tokens),
            format_amount(entry.cost_micros, config.currency_symbol),
            "*" if entry.estimated else "",
            entry.latency_ms,
            entry.endpoint,
            entry.request_id,
        ]
        for entry in entries
    ]
    print(
        format_table(
            ["时间", "Key", "状态", "输入", "缓存", "输出", "费用", "估算", "延迟ms", "端点", "请求ID"],
            rows,
            numeric=[2, 3, 4, 5, 6, 8],
        )
    )
    return 0


# -------------------------------------------------------------------- show-config
def cmd_show_config(args: argparse.Namespace) -> int:
    config = _load_config(args)
    print(json.dumps(config.public_view(), ensure_ascii=False, indent=2))
    return 0


# --------------------------------------------------------------------------- prune
def cmd_prune(args: argparse.Namespace) -> int:
    config, db = _config_and_db(args)
    if args.days < 1:
        raise UserError("--days 必须大于等于 1")
    before = int(time.time()) - args.days * 86400
    _confirm(
        args.yes,
        f"删除 {format_timestamp(before, config.timezone_offset_minutes)} 之前的请求明细？"
        "计费汇总不会删除 [y/N] ",
    )
    deleted = db.prune_requests(before)
    print(f"已删除 {deleted:,} 条请求明细；日/月计费汇总保持不变。")
    return 0


# ------------------------------------------------------------------------ helpers
def _load_config(args: argparse.Namespace) -> Config:
    explicit = bool(args.config or os.environ.get(CONFIG_ENV))
    return load_config(args.config, require_exists=explicit)


def _config_and_db(args: argparse.Namespace) -> tuple[Config, Database]:
    config = _load_config(args)
    db = Database(config.database_path, config.timezone_offset_minutes)
    db.initialize()
    return config, db


def _client_base_url(config: Config) -> str:
    host = config.host
    if host in {"0.0.0.0", "::"}:
        host = "127.0.0.1"
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"http://{host}:{config.port}"


def _key_state(key: ApiKey) -> str:
    if key.revoked_at is not None:
        return "已吊销"
    return "启用" if key.enabled else "停用"


def _limit_text(value: int | None) -> str:
    return "不限" if value is None else format_int(value)


def _cost_limit_text(value: int | None, config: Config) -> str:
    return "不限" if value is None else format_amount(value, config.currency_symbol)


def _models_text(allowed_models: tuple[str, ...] | None) -> str:
    if allowed_models is None:
        return "全部"
    if not allowed_models:
        return "禁止"
    rendered = "、".join(allowed_models)
    return rendered if len(rendered) <= 80 else rendered[:77] + "..."


def _remaining_text(limit: int | None, used: int) -> str:
    if limit is None:
        return "不限"
    return format_int(max(0, limit - used))


def _offset_text(offset_minutes: int) -> str:
    sign = "+" if offset_minutes >= 0 else "-"
    absolute = abs(offset_minutes)
    return f"{sign}{absolute // 60:02d}:{absolute % 60:02d}"


def _print_period_detail(
    label: str,
    totals: UsageTotals,
    token_limit: int | None,
    cost_limit: int | None,
    config: Config,
    reset_at: int,
) -> None:
    token_text = f"{format_int(totals.total_tokens)} / {_limit_text(token_limit)}"
    cost_text = (
        f"{format_amount(totals.cost_micros, config.currency_symbol)} / "
        f"{_cost_limit_text(cost_limit, config)}"
    )
    print(f"{label}：tokens {token_text}  缓存命中 {format_int(totals.cached_input_tokens)}  "
          f"费用 {cost_text}  请求 {totals.requests}")
    print(f"      重置时间 {format_timestamp(reset_at, config.timezone_offset_minutes)}")


def _sum_totals(items: Any) -> UsageTotals:
    input_tokens = output_tokens = requests = cost = cached = 0
    for item in items:
        input_tokens += item.input_tokens
        output_tokens += item.output_tokens
        requests += item.requests
        cost += item.cost_micros
        cached += item.cached_input_tokens
    return UsageTotals(input_tokens, output_tokens, requests, cost, cached)


def _totals_json(totals: UsageTotals) -> dict[str, int]:
    return {
        "input_tokens": totals.input_tokens,
        "output_tokens": totals.output_tokens,
        "cached_input_tokens": totals.cached_input_tokens,
        "total_tokens": totals.total_tokens,
        "requests": totals.requests,
        "cost_micros": totals.cost_micros,
    }


def _overview_json(item: KeyUsageOverview, config: Config) -> dict[str, Any]:
    return {
        "id": item.key.id,
        "name": item.key.name,
        "prefix": item.key.key_prefix,
        "enabled": item.key.enabled,
        "revoked": item.key.revoked_at is not None,
        "price_in_micros_per_mtok": item.key.price_in_micros_per_mtok,
        "price_out_micros_per_mtok": item.key.price_out_micros_per_mtok,
        "allowed_models": (
            list(item.key.allowed_models) if item.key.allowed_models is not None else None
        ),
        "day": {
            **_totals_json(item.day),
            "token_limit": item.key.daily_token_limit,
            "cost_limit_micros": item.key.daily_cost_limit_micros,
        },
        "month": {
            **_totals_json(item.month),
            "token_limit": item.key.monthly_token_limit,
            "cost_limit_micros": item.key.monthly_cost_limit_micros,
        },
        "currency": config.currency,
    }


def _confirm(skip: bool, prompt: str) -> None:
    if skip:
        return
    if not sys.stdin.isatty():
        raise UserError("非交互模式请显式添加 --yes")
    answer = input(prompt).strip().lower()
    if answer not in {"y", "yes"}:
        raise UserError("已取消")
