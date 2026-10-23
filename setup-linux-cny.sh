#!/usr/bin/env bash
# One-shot Linux setup for the CNY deployment profile:
#   - install with deploy.sh
#   - create/reuse three downstream keys with daily cost limits
#   - import model prices from 模型定价.md
#   - restart and verify the service
#
# Run as root from the repository root:
#   sudo ./setup-linux-cny.sh --upstream-base-url https://www.example.com/v1

set -Eeuo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SERVICE_NAME="token-router"
UPSTREAM_BASE_URL="https://www.example.com/v1"
HOST="127.0.0.1"
PORT="8787"
PYTHON_BIN="${PYTHON_BIN:-python3}"
PRICING_FILE="${SCRIPT_DIR}/模型定价.md"
ROTATE_KEYS=0
SKIP_DEPLOY=0
NO_RESTART=0
USERS=("user-a:60" "user-b:60" "user-c:20")
USERS_CUSTOM=0
HELP=0

usage() {
    cat <<'EOF'
TokenRouter Linux 一键设置（人民币日费用额度版）

用法：
  sudo ./setup-linux-cny.sh [选项]

默认会：
  1. 调用 deploy.sh 部署 systemd 服务（--no-key）
  2. 把用户 user-a/user-b/user-c 写入 /etc/token-router/config.json
  3. 每日费用额度分别为 ¥60 / ¥60 / ¥20
  4. 三个 key 均允许所有模型（["*"]）
  5. 从 模型定价.md 导入模型价格
  6. 重启并检查服务

选项：
  --upstream-base-url URL   上游 OpenAI 兼容地址（默认 https://www.example.com/v1）
  --host ADDR               监听地址（默认 127.0.0.1）
  --port PORT               监听端口（默认 8787）
  --service-name NAME       systemd 服务名（默认 token-router）
  --user NAME:CNY           自定义用户和每日费用额度，可重复；指定后会替换默认三项
  --pricing-file PATH       模型定价 Markdown（默认 ./模型定价.md）
  --python PATH             Python 3.10+（默认 python3）
  --rotate-keys             重新生成所有已配置用户的 key（旧 key 失效）
  --skip-deploy             跳过 deploy.sh，只更新现有配置/价格并重启
  --no-restart              不重启服务（用户配置需要手动重启才生效）
  -h, --help                显示帮助

上游 key：
  - 优先读取环境变量 TOKEN_ROUTER_UPSTREAM_KEY
  - 否则若已有 /etc/token-router/token-router.env 则保留
  - 否则脚本交互式提示输入（不会回显）
EOF
}

info() { printf '[setup] %s\n' "$*"; }
warn() { printf '[setup] 警告：%s\n' "$*" >&2; }
die() { printf '[setup] 错误：%s\n' "$*" >&2; exit 1; }
need_value() { [[ $# -ge 2 && -n "${2:-}" ]] || die "参数 $1 缺少值"; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --upstream-base-url) need_value "$@"; UPSTREAM_BASE_URL="$2"; shift 2 ;;
        --host) need_value "$@"; HOST="$2"; shift 2 ;;
        --port) need_value "$@"; PORT="$2"; shift 2 ;;
        --service-name) need_value "$@"; SERVICE_NAME="$2"; shift 2 ;;
        --user)
            need_value "$@"
            if [[ "$USERS_CUSTOM" -eq 0 ]]; then
                USERS=()
                USERS_CUSTOM=1
            fi
            USERS+=("$2")
            shift 2
            ;;
        --pricing-file) need_value "$@"; PRICING_FILE="$2"; shift 2 ;;
        --python) need_value "$@"; PYTHON_BIN="$2"; shift 2 ;;
        --rotate-keys) ROTATE_KEYS=1; shift ;;
        --skip-deploy) SKIP_DEPLOY=1; shift ;;
        --no-restart) NO_RESTART=1; shift ;;
        -h|--help) HELP=1; shift ;;
        *) die "未知参数：$1（使用 --help 查看帮助）" ;;
    esac
done

if [[ "$HELP" -eq 1 ]]; then
    usage
    exit 0
fi

[[ "${EUID}" -eq 0 ]] || die "请使用 sudo/root 运行"
[[ "$(uname -s)" == "Linux" ]] || die "此脚本仅支持 Linux"
[[ -n "$UPSTREAM_BASE_URL" ]] || die "upstream-base-url 不能为空"
[[ -n "$HOST" ]] || die "host 不能为空"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "port 必须是整数"
(( PORT >= 1024 && PORT <= 65535 )) || die "非 root 服务必须使用 1024-65535 的端口"
[[ "$SERVICE_NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || die "service-name 只能包含字母、数字、点、下划线、短横线"
[[ -r "$PRICING_FILE" ]] || die "模型定价文件不存在或不可读：$PRICING_FILE"
[[ "${#USERS[@]}" -gt 0 ]] || die "至少需要一个 --user NAME:CNY"

for spec in "${USERS[@]}"; do
    [[ "$spec" == *:* ]] || die "--user 格式应为 NAME:CNY，例如 user-a:60"
    name="${spec%%:*}"
    limit="${spec#*:}"
    [[ "$name" =~ ^[A-Za-z0-9_.-]+$ ]] || die "用户名只能包含字母、数字、点、下划线、短横线：$name"
    [[ "$limit" =~ ^[0-9]+([.][0-9]+)?$ ]] || die "用户 $name 的每日额度必须是数字：$limit"
done

PYTHON_BIN="$(command -v "$PYTHON_BIN" || true)"
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || die "找不到 Python"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "需要 Python 3.10+"
[[ -f "$SCRIPT_DIR/deploy.sh" ]] || die "缺少 deploy.sh"
[[ -f "$SCRIPT_DIR/token_router/pricing.py" ]] || die "缺少 token_router/pricing.py（请在仓库根目录运行）"

CONFIG_FILE="/etc/token-router/config.json"
ENV_FILE="/etc/token-router/token-router.env"
ADMIN_BIN="/usr/local/bin/${SERVICE_NAME}-admin"

UPSTREAM_KEY="${TOKEN_ROUTER_UPSTREAM_KEY:-}"
if [[ "$SKIP_DEPLOY" -eq 0 ]]; then
    if [[ -z "$UPSTREAM_KEY" && ! -s "$ENV_FILE" ]]; then
        [[ -t 0 ]] || die "非交互环境请设置 TOKEN_ROUTER_UPSTREAM_KEY 环境变量"
        read -rsp "请输入上游 API key（输入不会回显）: " UPSTREAM_KEY
        printf '\n'
    fi
    if [[ -n "$UPSTREAM_KEY" ]]; then
        export TOKEN_ROUTER_UPSTREAM_KEY="$UPSTREAM_KEY"
    fi
    info "调用 deploy.sh 部署/升级 systemd 服务"
    bash "$SCRIPT_DIR/deploy.sh" \
        --upstream-base-url "$UPSTREAM_BASE_URL" \
        --host "$HOST" \
        --port "$PORT" \
        --service-name "$SERVICE_NAME" \
        --python "$PYTHON_BIN" \
        --no-key
fi

[[ -f "$CONFIG_FILE" ]] || die "配置文件不存在：$CONFIG_FILE"
[[ -x "$ADMIN_BIN" ]] || die "管理命令不存在：$ADMIN_BIN"

info "更新配置：人民币、3 个用户、每日费用额度和全部模型权限"
KEY_OUTPUT="$(
    "$PYTHON_BIN" - "$CONFIG_FILE" "$UPSTREAM_BASE_URL" "$HOST" "$PORT" "$ROTATE_KEYS" "${USERS[@]}" <<'PY'
import json
import os
import secrets
import sys

path, base, host, port_raw, rotate_raw, *specs = sys.argv[1:]
port = int(port_raw)
rotate = rotate_raw == "1"

with open(path, "r", encoding="utf-8") as handle:
    config = json.load(handle)

config["upstream_base_url"] = base
config["host"] = host
config["port"] = port
config["currency"] = "CNY"
config["currency_symbol"] = "¥"

users = [item for item in (config.get("users") or []) if isinstance(item, dict)]
by_name = {item.get("name"): item for item in users if item.get("name")}
configured = []
new_keys = []

for spec in specs:
    name, limit = spec.rsplit(":", 1)
    existing = by_name.get(name)
    if existing and not rotate and existing.get("api_key"):
        api_key = str(existing["api_key"])
    else:
        api_key = "tr_" + secrets.token_urlsafe(32)
    created = not existing or rotate
    entry = dict(existing or {})
    entry.pop("key", None)
    entry.update(
        {
            "name": name,
            "api_key": api_key,
            "daily_token_limit": None,
            "monthly_token_limit": None,
            "daily_cost_limit": float(limit),
            "monthly_cost_limit": None,
            "allowed_models": ["*"],
            "enabled": True,
            "note": f"daily CNY {limit}",
        }
    )
    configured.append(entry)
    if created:
        new_keys.append((name, api_key))

configured_names = {item["name"] for item in configured}
others = [item for item in users if item.get("name") not in configured_names]
config["users"] = others + configured

with open(path, "w", encoding="utf-8") as handle:
    json.dump(config, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
os.chmod(path, 0o640)

for name, api_key in new_keys:
    print(f"{name}\t{api_key}")
PY
)" || die "更新配置文件失败"

CONFIG_GROUP="$(id -gn "$SERVICE_NAME" 2>/dev/null || true)"
[[ -n "$CONFIG_GROUP" ]] || die "找不到服务组：$SERVICE_NAME"
chown root:"$CONFIG_GROUP" "$CONFIG_FILE"
chmod 0640 "$CONFIG_FILE"

if [[ -n "$KEY_OUTPUT" ]]; then
    printf '\n本次生成/轮换的下游 key（只显示一次，请立即保存）：\n'
    while IFS=$'\t' read -r name api_key; do
        [[ -z "$name" ]] && continue
        printf '  %-12s %s\n' "$name" "$api_key"
    done <<< "$KEY_OUTPUT"
    printf '\n'
else
    info "保留现有下游 key（配置文件中已有同名用户；如需轮换加 --rotate-keys）"
fi

if [[ "$NO_RESTART" -eq 0 ]]; then
    info "重启服务并等待用户配置同步"
    systemctl restart "$SERVICE_NAME"
    ACTIVE=0
    for _ in 1 2 3 4 5 6 7 8 9 10; do
        if systemctl is-active --quiet "$SERVICE_NAME"; then
            ACTIVE=1
            break
        fi
        sleep 0.5
    done
    if [[ "$ACTIVE" -ne 1 ]]; then
        systemctl --no-pager --full status "$SERVICE_NAME" || true
        journalctl -u "$SERVICE_NAME" -n 50 --no-pager || true
        die "服务启动失败"
    fi
fi

info "从 $PRICING_FILE 导入模型价格"
PRICE_ROWS="$(
    PYTHONPATH="$SCRIPT_DIR" "$PYTHON_BIN" -m token_router.pricing "$PRICING_FILE"
)" || die "解析模型定价文件失败"
[[ -n "$PRICE_ROWS" ]] || die "模型定价文件没有解析到价格"

PRICE_COUNT=0
while IFS='|' read -r model input output cached; do
    [[ -z "$model" ]] && continue
    "$ADMIN_BIN" price set \
        --model "$model" \
        --input "$input" \
        --output "$output" \
        --cached "$cached"
    PRICE_COUNT=$((PRICE_COUNT + 1))
done <<< "$PRICE_ROWS"

HEALTH_HOST="$HOST"
if [[ "$HEALTH_HOST" == "0.0.0.0" || "$HEALTH_HOST" == "::" || "$HEALTH_HOST" == "[::]" ]]; then
    HEALTH_HOST="127.0.0.1"
fi
if [[ "$HEALTH_HOST" == *:* && "$HEALTH_HOST" != \[*\] ]]; then
    HEALTH_HOST="[${HEALTH_HOST}]"
fi
if [[ "$NO_RESTART" -eq 0 ]] && command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time 3 "http://${HEALTH_HOST}:${PORT}/health" >/dev/null 2>&1; then
        info "健康检查通过：http://${HEALTH_HOST}:${PORT}/health"
    else
        warn "健康检查未通过，请查看 journalctl -u ${SERVICE_NAME}"
    fi
fi

printf '\n'
printf '============== TokenRouter CNY 设置完成 ==============\n'
printf '服务：      %s\n' "$SERVICE_NAME"
printf '配置：      %s\n' "$CONFIG_FILE"
printf '接入地址：  http://%s:%s/v1\n' "$HEALTH_HOST" "$PORT"
printf '模型价格：  已导入 %s 个模型（全局价，人民币/百万 token）\n' "$PRICE_COUNT"
printf '用户价格：  全部模型允许（["*"]）\n'
printf '\n验证命令：\n'
printf '  sudo %s key list\n' "$ADMIN_BIN"
printf '  sudo %s price list\n' "$ADMIN_BIN"
printf '  sudo %s usage\n' "$ADMIN_BIN"
printf '  sudo journalctl -u %s -n 30 --no-pager\n' "$SERVICE_NAME"
printf '\n提示：如果之前已经创建过默认 key，可先执行 key list 查看，再按需 key revoke。\n'
if [[ "$NO_RESTART" -eq 1 ]]; then
    printf '\n注意：已使用 --no-restart，文件用户尚未同步；请执行：sudo systemctl restart %s\n' "$SERVICE_NAME"
fi
if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" ]]; then
    printf '\n安全提醒：服务当前监听 %s，未提供 TLS；请使用反向代理和防火墙。\n' "$HOST"
fi
