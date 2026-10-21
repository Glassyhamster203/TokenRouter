#!/usr/bin/env bash
# One-shot Linux/systemd deployment for TokenRouter.
# Zero third-party Python dependencies: only the system Python 3.10+ is used.

set -Eeuo pipefail

SERVICE_NAME="${SERVICE_NAME:-token-router}"
INSTALL_DIR="${INSTALL_DIR:-/opt/token-router}"
CONFIG_DIR="${CONFIG_DIR:-/etc/token-router}"
DATA_DIR="${DATA_DIR:-/var/lib/token-router}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8787}"
UPSTREAM_BASE_URL="${UPSTREAM_BASE_URL:-https://api.openai.com/v1}"
TIMEZONE_OFFSET_MINUTES="${TIMEZONE_OFFSET_MINUTES:-480}"
KEY_NAME="${KEY_NAME:-default}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
CREATE_KEY=1
NO_START=0
FORCE_CONFIG=0
FORCE_KEY=0

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<'EOF'
TokenRouter Linux 一键部署（systemd）

用法：
  sudo ./deploy.sh [选项]

常用选项：
  --upstream-base-url URL   上游 OpenAI 兼容地址（默认 https://api.openai.com/v1）
  --host ADDR               监听地址（默认 127.0.0.1；对外暴露前请加 TLS 反代）
  --port PORT               监听端口（默认 8787，必须 >= 1024）
  --install-dir PATH        程序目录（默认 /opt/token-router）
  --config-dir PATH         配置目录（默认 /etc/token-router）
  --data-dir PATH           数据目录（默认 /var/lib/token-router）
  --service-name NAME       systemd 服务名/系统用户名（默认 token-router）
  --python PATH             Python 3.10+ 解释器（默认 python3）
  --timezone-offset-minutes N  计费时区偏移（默认 480 = UTC+08:00）
  --key-name NAME           首次安装时创建的下游 key 名称（默认 default）
  --no-key                  不自动创建首个下游 key
  --no-start                安装并 enable，但不立即启动
  --force-config            重新生成配置（会覆盖 host/port/upstream 配置）
  --force-key               强制更新上游 key（默认优先读取环境变量）
  -h, --help                显示帮助

上游 key 的来源（按优先级）：
  1. 环境变量 TOKEN_ROUTER_UPSTREAM_KEY
  2. 命令行交互输入
  3. 已存在的 /etc/token-router/token-router.env
升级时若没有提供新 key，会保留现有 key。

示例：
  sudo ./deploy.sh --upstream-base-url https://api.openai.com/v1 --port 8787
  脚本会安全地提示输入上游 key，不会把 key 写进命令行参数。
EOF
}

info() { printf '[TokenRouter] %s\n' "$*"; }
warn() { printf '[TokenRouter] 警告：%s\n' "$*" >&2; }
die() { printf '[TokenRouter] 错误：%s\n' "$*" >&2; exit 1; }

need_value() {
    [[ $# -ge 2 && -n "${2:-}" ]] || die "参数 $1 缺少值"
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --upstream-base-url) need_value "$@"; UPSTREAM_BASE_URL="$2"; shift 2 ;;
        --host) need_value "$@"; HOST="$2"; shift 2 ;;
        --port) need_value "$@"; PORT="$2"; shift 2 ;;
        --install-dir) need_value "$@"; INSTALL_DIR="$2"; shift 2 ;;
        --config-dir) need_value "$@"; CONFIG_DIR="$2"; shift 2 ;;
        --data-dir) need_value "$@"; DATA_DIR="$2"; shift 2 ;;
        --service-name) need_value "$@"; SERVICE_NAME="$2"; shift 2 ;;
        --python) need_value "$@"; PYTHON_BIN="$2"; shift 2 ;;
        --timezone-offset-minutes) need_value "$@"; TIMEZONE_OFFSET_MINUTES="$2"; shift 2 ;;
        --key-name) need_value "$@"; KEY_NAME="$2"; shift 2 ;;
        --no-key) CREATE_KEY=0; shift ;;
        --no-start) NO_START=1; shift ;;
        --force-config) FORCE_CONFIG=1; shift ;;
        --force-key) FORCE_KEY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) die "未知参数：$1（使用 --help 查看帮助）" ;;
    esac
done

[[ "${EUID}" -eq 0 ]] || die "请使用 sudo/root 运行（需要 systemd、创建用户和写 /etc）"
[[ "$(uname -s)" == "Linux" ]] || die "此脚本仅支持 Linux"
[[ -d /run/systemd/system ]] || die "未检测到运行中的 systemd；请改用源码方式运行"
command -v systemctl >/dev/null 2>&1 || die "缺少 systemctl"
command -v runuser >/dev/null 2>&1 || die "缺少 runuser（util-linux）"
[[ -n "$SERVICE_NAME" && "$SERVICE_NAME" =~ ^[A-Za-z0-9_.-]+$ ]] || die "service-name 只能包含字母、数字、点、下划线、短横线"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "port 必须是整数"
(( PORT >= 1024 && PORT <= 65535 )) || die "非 root 服务必须使用 1024-65535 的端口"
[[ "$TIMEZONE_OFFSET_MINUTES" =~ ^-?[0-9]+$ ]] || die "timezone-offset-minutes 必须是整数"
(( TIMEZONE_OFFSET_MINUTES >= -720 && TIMEZONE_OFFSET_MINUTES <= 840 )) || die "时区偏移超出范围"
[[ -n "$HOST" ]] || die "host 不能为空"
[[ -n "$UPSTREAM_BASE_URL" ]] || die "upstream-base-url 不能为空"
[[ -n "$KEY_NAME" ]] || die "key-name 不能为空"
[[ "$KEY_NAME" != *$'\n'* && "$KEY_NAME" != *$'\r'* ]] || die "key-name 不能包含换行"

for path in "$INSTALL_DIR" "$CONFIG_DIR" "$DATA_DIR"; do
    [[ "$path" == /* ]] || die "目录必须是绝对路径：$path"
    [[ "$path" != *" "* && "$path" != *$'\t'* && "$path" != *$'\n'* ]] || die "目录路径不能包含空白字符：$path"
    [[ "$path" != *"/../"* && "$path" != *"/.." ]] || die "目录路径不能包含 ..：$path"
done
[[ "$INSTALL_DIR" != "/" && "$CONFIG_DIR" != "/" && "$DATA_DIR" != "/" ]] || die "不能把根目录作为安装/配置/数据目录"
INSTALL_DIR="${INSTALL_DIR%/}"
CONFIG_DIR="${CONFIG_DIR%/}"
DATA_DIR="${DATA_DIR%/}"
[[ -n "$INSTALL_DIR" && -n "$CONFIG_DIR" && -n "$DATA_DIR" ]] || die "安装/配置/数据目录不能为空"
[[ "$INSTALL_DIR" != "/" && "$CONFIG_DIR" != "/" && "$DATA_DIR" != "/" ]] || die "不能把根目录作为安装/配置/数据目录"

PYTHON_BIN="$(command -v "$PYTHON_BIN" || true)"
[[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]] || die "找不到可执行的 Python：$PYTHON_BIN"
[[ "$PYTHON_BIN" != *" "* && "$PYTHON_BIN" != *$'\t'* ]] || die "Python 路径不能包含空白字符"
"$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' \
    || die "需要 Python 3.10 或更高版本"
[[ -f "$SCRIPT_DIR/token_router/__main__.py" ]] || die "请在 TokenRouter 源码根目录运行（缺少 token_router/）"
[[ -f "$SCRIPT_DIR/config.json" ]] || die "缺少 config.json 模板"

SERVICE_USER="$SERVICE_NAME"
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
ENV_FILE="${CONFIG_DIR}/${SERVICE_NAME}.env"
CONFIG_FILE="${CONFIG_DIR}/config.json"
ADMIN_BIN="/usr/local/bin/${SERVICE_NAME}-admin"

command -v getent >/dev/null 2>&1 || die "缺少 getent"
if ! getent group "$SERVICE_USER" >/dev/null 2>&1; then
    command -v groupadd >/dev/null 2>&1 || die "缺少 groupadd"
    groupadd --system "$SERVICE_USER"
fi
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
    command -v useradd >/dev/null 2>&1 || die "缺少 useradd"
    NOLOGIN="$(command -v nologin || true)"
    [[ -n "$NOLOGIN" ]] || NOLOGIN="/usr/sbin/nologin"
    useradd --system --gid "$SERVICE_USER" --home-dir "$INSTALL_DIR" \
        --no-create-home --shell "$NOLOGIN" "$SERVICE_USER"
fi
SERVICE_GROUP="$(id -gn "$SERVICE_USER")"

info "安装目录：$INSTALL_DIR"
info "配置目录：$CONFIG_DIR"
info "数据目录：$DATA_DIR"

install -d -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$DATA_DIR"

# Replace only the package, so upgrades do not leave stale modules behind.
rm -rf "$INSTALL_DIR/token_router"
cp -a "$SCRIPT_DIR/token_router" "$INSTALL_DIR/token_router"
find "$INSTALL_DIR/token_router" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
chown -R root:root "$INSTALL_DIR/token_router"
chmod -R u=rwX,go=rX "$INSTALL_DIR/token_router"

if [[ -f "$CONFIG_FILE" && "$FORCE_CONFIG" -eq 0 ]]; then
    info "保留现有配置：$CONFIG_FILE（--force-config 可覆盖）"
else
    "$PYTHON_BIN" - "$SCRIPT_DIR/config.json" "$CONFIG_FILE" \
        "$HOST" "$PORT" "$UPSTREAM_BASE_URL" "$DATA_DIR" "$TIMEZONE_OFFSET_MINUTES" <<'PY'
import json
import sys

source, target, host, port, base_url, data_dir, offset = sys.argv[1:]
with open(source, "r", encoding="utf-8") as handle:
    document = json.load(handle)
document.update(
    {
        "host": host,
        "port": int(port),
        "upstream_base_url": base_url,
        "data_dir": data_dir,
        "timezone_offset_minutes": int(offset),
        "log_level": "INFO",
    }
)
with open(target, "w", encoding="utf-8") as handle:
    json.dump(document, handle, ensure_ascii=False, indent=2)
    handle.write("\n")
PY
    chown root:root "$CONFIG_FILE"
    chmod 0644 "$CONFIG_FILE"
    info "已写入配置：$CONFIG_FILE"
fi

mapfile -t CONFIG_INFO < <(
    "$PYTHON_BIN" - "$CONFIG_FILE" "$INSTALL_DIR" <<'PY'
import sys

sys.path.insert(0, sys.argv[2])
from token_router.config import load_config

config = load_config(sys.argv[1], require_exists=True)
print(config.data_dir)
print(config.database_path)
PY
)
CONFIG_DATA_DIR="${CONFIG_INFO[0]:-}"
DB_PATH="${CONFIG_INFO[1]:-}"
[[ -n "$CONFIG_DATA_DIR" && -n "$DB_PATH" ]] || die "无法从配置解析数据目录/数据库路径"
[[ "$CONFIG_DATA_DIR" == /* && "$CONFIG_DATA_DIR" != "/" ]] || die "配置中的 data_dir 必须是绝对的非根目录"
[[ "$CONFIG_DATA_DIR" != *" "* && "$CONFIG_DATA_DIR" != *$'\n'* ]] || die "配置中的 data_dir 不能包含空白字符"
[[ "$CONFIG_DATA_DIR" != *"/../"* && "$CONFIG_DATA_DIR" != *"/.." ]] || die "配置中的 data_dir 不能包含 .."
case "$CONFIG_DATA_DIR" in
    /etc|/usr|/bin|/sbin|/lib|/lib64|/boot|/dev|/proc|/sys|/var|/opt|/home|/root)
        die "拒绝把系统目录作为 data_dir：$CONFIG_DATA_DIR" ;;
esac
install -d -m 0750 -o "$SERVICE_USER" -g "$SERVICE_GROUP" "$CONFIG_DATA_DIR"
DB_EXISTED=0
if [[ -f "$DB_PATH" ]]; then
    DB_EXISTED=1
fi

if [[ -n "${TOKEN_ROUTER_UPSTREAM_KEY:-}" || ! -s "$ENV_FILE" || "$FORCE_KEY" -eq 1 ]]; then
    if [[ -z "${TOKEN_ROUTER_UPSTREAM_KEY:-}" ]]; then
        [[ -t 0 ]] || die "非交互环境请设置 TOKEN_ROUTER_UPSTREAM_KEY 环境变量"
        read -rsp "请输入唯一的上游 API key（输入不会回显）: " TOKEN_ROUTER_UPSTREAM_KEY
        printf '\n'
    fi
    [[ -n "$TOKEN_ROUTER_UPSTREAM_KEY" ]] || die "上游 API key 不能为空"
    [[ "$TOKEN_ROUTER_UPSTREAM_KEY" != *$'\n'* && "$TOKEN_ROUTER_UPSTREAM_KEY" != *$'\r'* ]] \
        || die "上游 API key 不能包含换行"
    umask 077
    KEY_ESCAPED="${TOKEN_ROUTER_UPSTREAM_KEY//\\/\\\\}"
    KEY_ESCAPED="${KEY_ESCAPED//\"/\\\"}"
    printf 'TOKEN_ROUTER_UPSTREAM_KEY="%s"\n' "$KEY_ESCAPED" > "$ENV_FILE"
    chown root:root "$ENV_FILE"
    chmod 0600 "$ENV_FILE"
    info "已更新上游 key：$ENV_FILE（权限 0600）"
else
    info "保留现有上游 key：$ENV_FILE"
fi

# Initialise/migrate the ledger as the service user so SQLite files have the
# correct owner from the start.
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$CONFIG_DATA_DIR"
runuser -u "$SERVICE_USER" -- env PYTHONPATH="$INSTALL_DIR" "$PYTHON_BIN" - \
    "$CONFIG_FILE" "$INSTALL_DIR" <<'PY'
import sys

sys.path.insert(0, sys.argv[2])
from token_router.config import load_config
from token_router.db import Database

config = load_config(sys.argv[1], require_exists=True)
database = Database(config.database_path, config.timezone_offset_minutes)
database.initialize()
database.close_thread_connection()
PY
chown -R "$SERVICE_USER:$SERVICE_GROUP" "$CONFIG_DATA_DIR"
chmod 0750 "$CONFIG_DATA_DIR"

FIRST_KEY_OUTPUT=""
if [[ "$DB_EXISTED" -eq 0 && "$CREATE_KEY" -eq 1 ]]; then
    info "创建首个下游 key：$KEY_NAME"
    FIRST_KEY_OUTPUT="$(
        runuser -u "$SERVICE_USER" -- env PYTHONPATH="$INSTALL_DIR" \
            "$PYTHON_BIN" -m token_router --config "$CONFIG_FILE" \
            key create --name "$KEY_NAME"
    )"
fi

cat > "$UNIT_FILE" <<EOF
[Unit]
Description=TokenRouter OpenAI-compatible metered gateway
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_GROUP}
WorkingDirectory=${INSTALL_DIR}
EnvironmentFile=${ENV_FILE}
Environment=PYTHONPATH=${INSTALL_DIR}
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=PYTHONNOUSERSITE=1
ExecStart=${PYTHON_BIN} -m token_router serve --config ${CONFIG_FILE}
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
NoNewPrivileges=true
PrivateTmp=true
PrivateDevices=true
ProtectSystem=strict
ProtectHome=true
ProtectClock=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=true
RestrictSUIDSGID=true
LockPersonality=true
MemoryDenyWriteExecute=true
RemoveIPC=true
CapabilityBoundingSet=
UMask=0077
LimitNOFILE=65535
ReadWritePaths=${CONFIG_DATA_DIR}

[Install]
WantedBy=multi-user.target
EOF
chmod 0644 "$UNIT_FILE"
chown root:root "$UNIT_FILE"

cat > "$ADMIN_BIN" <<EOF
#!/bin/sh
set -eu
if [ "\$(id -u)" -ne 0 ]; then
    echo "请使用 sudo ${SERVICE_NAME}-admin ..." >&2
    exit 1
fi
exec runuser -u ${SERVICE_USER} -- env PYTHONPATH=${INSTALL_DIR} \\
    PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 ${PYTHON_BIN} \\
    -m token_router --config ${CONFIG_FILE} "\$@"
EOF
chmod 0755 "$ADMIN_BIN"
chown root:root "$ADMIN_BIN"

systemctl daemon-reload
systemctl enable "$SERVICE_NAME" >/dev/null

if [[ "$NO_START" -eq 0 ]]; then
    info "启动服务：$SERVICE_NAME"
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
        die "服务启动失败，请查看上面的 systemd 日志"
    fi
    HEALTH_HOST="$HOST"
    if [[ "$HEALTH_HOST" == "0.0.0.0" || "$HEALTH_HOST" == "::" || "$HEALTH_HOST" == "[::]" ]]; then
        HEALTH_HOST="127.0.0.1"
    fi
    if [[ "$HEALTH_HOST" == *:* && "$HEALTH_HOST" != \[*\] ]]; then
        HEALTH_HOST="[${HEALTH_HOST}]"
    fi
    if command -v curl >/dev/null 2>&1; then
        if curl -fsS --max-time 3 "http://${HEALTH_HOST}:${PORT}/health" >/dev/null 2>&1; then
            info "健康检查通过：http://${HEALTH_HOST}:${PORT}/health"
        else
            warn "服务已启动，但本地健康检查未通过，请检查日志"
        fi
    fi
else
    info "已 enable 但未启动（--no-start）。启动：systemctl start $SERVICE_NAME"
fi

printf '\n'
printf '================ TokenRouter 部署完成 ================\n'
DISPLAY_HOST="$HOST"
if [[ "$DISPLAY_HOST" == *:* && "$DISPLAY_HOST" != \[*\] ]]; then
    DISPLAY_HOST="[${DISPLAY_HOST}]"
fi
printf '服务：      %s\n' "$SERVICE_NAME"
printf '程序：      %s\n' "$INSTALL_DIR"
printf '配置：      %s\n' "$CONFIG_FILE"
printf '数据：      %s\n' "$CONFIG_DATA_DIR"
printf '接入地址：  http://%s:%s/v1\n' "$DISPLAY_HOST" "$PORT"
printf '管理命令：  sudo %s-admin status\n' "$SERVICE_NAME"
printf '查看日志：  sudo journalctl -u %s -f\n' "$SERVICE_NAME"
if [[ "$HOST" != "127.0.0.1" && "$HOST" != "localhost" && "$HOST" != "::1" ]]; then
    printf '安全提醒：  当前监听 %s。本服务不提供 TLS，暴露到局域网/公网前请加反向代理和防火墙。\n' "$HOST"
fi
if [[ -n "$FIRST_KEY_OUTPUT" ]]; then
    printf '\n首次创建的下游 key（明文只显示这一次，请立即保存）：\n'
    printf '%s\n' "$FIRST_KEY_OUTPUT"
    printf '\n后续请设置价格和限额，例如：\n'
    printf '  sudo %s-admin key update 1 --price-in 0.5 --price-out 1.5 --daily-tokens 2m\n' "$SERVICE_NAME"
    printf '  sudo %s-admin price set --model gpt-4o-mini --input 0.15 --output 0.60 --cached 0.075\n' "$SERVICE_NAME"
fi
