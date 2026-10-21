# TokenRouter

一个**单端口、零第三方运行时依赖**的 OpenAI 兼容 token 路由与计费网关。

它把多个下游应用/租户的虚拟 key，统一汇聚到**唯一一个上游 API key**；所有下游只连接本机的一个端口。路由器按每个下游 key 记录输入/输出 token、计算费用，并在转发前检查日/月 token 配额与费用配额。

> 注意：这里的“多个 key 路由到一个 key”指**多个下游虚拟 key 共用一个真实上游 key**，不是对多个上游 key 做负载均衡。

```text
下游应用 A ─┐
下游应用 B ─┼─ Bearer tr_xxx ──> 127.0.0.1:8787（唯一端口） ──> 唯一上游 key ──> OpenAI 兼容 API
下游应用 C ─┘                         │
                                      ├─ SQLite：虚拟 key / 日、月用量桶 / 请求账本
                                      └─ 本地 CLI：建 key、改限额、查账单（不开放管理端口）
```

## 特性

- **单端口接入**：所有下游 key 都通过 `http://127.0.0.1:8787/v1` 连接；管理操作走本地 CLI，不额外监听端口。
- **真实上游 key 可放文件或环境变量**：支持在 JSON/TOML 中声明上游 URL、上游 key；生产环境仍推荐 systemd EnvironmentFile。
- **文件配置用户**：JSON/TOML 中可声明用户 key、日/月 token 与费用额度、可用模型；服务启动时按用户名同步到 SQLite。
- **首次启动自动生成配置**：`serve` 找不到配置文件时会自动生成带默认参数的文件；`init` 支持 JSON/TOML。
- **模型权限**：每个 key 可限制允许的模型，支持精确名称和 `fnmatch` 通配模式（如 `gpt-4o*`）；`[]` 表示禁止全部。
- **虚拟 key**：随机生成、只在创建时显示一次，数据库只保存 SHA-256 哈希和前 12 位前缀。
- **请求级计费**：记录每个请求的模型、端点、输入/缓存命中/输出 token、费用、延迟、状态和是否估算。
- **模型级价格**：支持全局模型价和按 key 覆盖价，分别设置输入、输出、缓存命中（每百万 token）单价；未匹配时回退到 key 默认价。
- **日/月双重限额**：每个 key 可独立设置每日、每月 token 限额和费用限额；两类限额可同时生效。
- **并发安全预占**：转发前预占 token/费用预算，避免多个并发请求同时穿过配额。429 响应带 `Retry-After`。
- **流式支持**：原样转发 SSE，同时从流中提取 `usage`；对 OpenAI chat/completions 自动注入 `stream_options.include_usage=true`。
- **精确优先、估算兜底**：优先使用上游返回的 usage；没有 usage 时用无分词器的保守估算，并在账本中标记 `estimated=1`。
- **零第三方依赖**：只使用 Python 标准库（`http.server`、`http.client`、`sqlite3`、`ssl`、`tomllib` 等）；测试使用 `unittest`。
- **Linux 一键部署**：自带 `deploy.sh`，自动创建系统用户、安装代码、生成配置和 systemd 服务，并以 0600 权限保存上游 key。
- **本地优先安全默认**：默认只绑定 `127.0.0.1`；非回环绑定会打印警告。

## 快速开始

### 1. 初始化

环境已指定为 `F:\uVenvs\ML_venv\Scripts\python.exe`。仓库根目录提供了一个 `run.cmd`，默认使用该解释器：

```powershell
cd E:\TokenRouter
.\run.cmd init
```

也可以直接调用 Python：

```powershell
& 'F:\uVenvs\ML_venv\Scripts\python.exe' -m token_router init
```

初始化会创建/保留 [config.json](config.json)，并创建 `data/router.db`。

### 2. 设置唯一的上游 key

PowerShell（当前窗口有效）：

```powershell
$env:TOKEN_ROUTER_UPSTREAM_KEY = "sk-你的真实上游key"
```

CMD（当前窗口有效）：

```bat
set TOKEN_ROUTER_UPSTREAM_KEY=sk-你的真实上游key
```

需要持久化时可以用 `setx`，但它只对新开的终端生效。上游 key 也可以直接写在配置文件 `upstream_api_key`（不推荐提交到 Git）；环境变量优先级更高。

### 3. 创建下游 key

```powershell
.\run.cmd key create --name "team-a" `
  --daily-tokens 2m --monthly-tokens 30m `
  --daily-cost 5 --monthly-cost 80 `
  --price-in 0.50 --price-out 1.50
```

- `--daily-tokens` / `--monthly-tokens` 支持 `100k`、`1.5m`、`2b`，`none` 表示不限。
- `--daily-cost` / `--monthly-cost` 的单位由 `config.json` 的 `currency` 决定；`none` 表示不限。
- `--price-in` / `--price-out` 是**key 默认**的每 100 万 token 价格，作为未配置模型价时的回退价。
- 明文 key 只显示一次，请立即保存。

按模型设置更精确的价格（可分别设置缓存命中价）：

```powershell
# 全局模型价，适用于所有 key
.\run.cmd price set --model gpt-4o-mini --input 0.15 --output 0.60 --cached 0.075

# 仅覆盖 key #1 的该模型价格
.\run.cmd price set --key 1 --model gpt-4o-mini --input 0.20 --output 0.80 --cached 0.10

# 查看价格
.\run.cmd price list
.\run.cmd price list --key 1
```

价格优先级：**key 模型覆盖价 > 全局模型价 > key 默认输入/输出价**。缓存命中价未设置时，默认等于输入价。

### 4. 启动网关

```powershell
.\run.cmd serve
```

默认监听 `127.0.0.1:8787`。启动后日志会打印监听地址、上游地址和数据库路径。

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8787/health
```

### 5. 下游调用

```powershell
$key = "tr_创建时显示的key"

curl.exe http://127.0.0.1:8787/v1/chat/completions `
  -H "Authorization: Bearer $key" `
  -H "Content-Type: application/json" `
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"你好"}],"max_tokens":128}'
```

Python OpenAI SDK（SDK 是下游依赖，不是路由器依赖）：

```python
from openai import OpenAI

client = OpenAI(
    api_key="tr_创建时显示的key",
    base_url="http://127.0.0.1:8787/v1",
)
response = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "你好"}],
    max_tokens=128,
)
print(response.choices[0].message.content)
```

流式调用：

```bash
curl -N http://127.0.0.1:8787/v1/chat/completions \
  -H "Authorization: Bearer tr_xxx" \
  -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

## 文件配置（JSON / TOML）

命令行适合临时建 key；长期、可版本管理的用户与额度建议直接写在配置文件里。

### 首次启动自动生成

如果 `serve` 启动时找不到配置文件，会自动生成默认 JSON 配置（显式传入 `.toml` 路径时生成 TOML）：

```powershell
# 生成 config.json（默认参数，users 为空）
.\run.cmd serve

# 指定 TOML 路径；不存在则生成默认 TOML
.\run.cmd --config config.toml serve
```

自动生成后需要在文件中填写 `upstream_api_key`（或设置环境变量），并按需添加 `users`。`init --format toml` 也可以直接生成 TOML：

```powershell
.\run.cmd init --format toml
```

默认路径会变成 `config.toml`。完整的 TOML 示例见 [config.example.toml](config.example.toml)。`config.toml` 默认被 `.gitignore` 忽略，避免误提交其中的 key；如需版本化配置，请使用不含真实 key 的模板或环境变量。

### TOML 示例

```toml
host = "127.0.0.1"
port = 8787
upstream_base_url = "https://api.openai.com/v1"
upstream_api_key = ""                    # 也可用环境变量
data_dir = "data"

[[users]]
name = "team-a"
api_key = "tr_替换为下游key"
daily_token_limit = "2m"                 # 支持 100k/2m/整数
monthly_token_limit = "30m"
daily_cost_limit = 5.0                   # 单位同 currency
monthly_cost_limit = 80.0
allowed_models = ["gpt-4o-mini", "gpt-4o*"]
enabled = true
note = "team a"

[[users]]
name = "readonly"
api_key = "tr_另一个下游key"
daily_token_limit = 100_000
allowed_models = []                      # 禁止所有模型
```

### JSON 示例

```json
{
  "upstream_base_url": "https://api.openai.com/v1",
  "upstream_api_key": "",
  "port": 8787,
  "users": [
    {
      "name": "team-a",
      "api_key": "tr_替换为下游key",
      "daily_token_limit": "2m",
      "monthly_token_limit": "30m",
      "daily_cost_limit": 5,
      "monthly_cost_limit": 80,
      "allowed_models": ["gpt-4o-mini", "gpt-4o*"],
      "enabled": true,
      "note": "team a"
    }
  ]
}
```

### 用户字段

| 字段 | 必填 | 说明 |
|---|---|---|
| `name` | 是 | 用户/key 名称；文件中必须唯一，也是启动同步的匹配键 |
| `api_key`（或别名 `key`） | 是 | 下游明文 key，至少 16 字符；同步后数据库只保存哈希 |
| `daily_token_limit` | 否 | 每日 token 限额；整数或 `"2m"`/`"100k"`，`null` 为不限 |
| `monthly_token_limit` | 否 | 每月 token 限额 |
| `daily_cost_limit` | 否 | 每日费用限额，单位同 `currency`，可写 `5.5` |
| `monthly_cost_limit` | 否 | 每月费用限额 |
| `allowed_models` | 否 | 允许的模型/通配模式；省略或 `null` 为全部允许，`[]` 为全部禁止，`["*"]` 等价全部允许 |
| `enabled` | 否 | 默认 `true`；`false` 会停用 key |
| `note` | 否 | 备注 |

模型匹配使用 Python `fnmatch` 语义（大小写敏感），例如 `gpt-4o*`、`o3*`。权限在生成类请求（chat/completions/embeddings/responses）转发前检查，不在流中途截断。

### 同步语义

- 服务启动时（`serve`/systemd restart）按 `name` upsert 文件中的用户。
- 同名用户会用文件内容更新 key、额度、模型权限、启用状态和备注；**旧的下游 key 会立即失效**。
- 文件中删除某个用户不会删除数据库里的 key，也不会删除历史账本；需要显式 `key disable/revoke`。
- 已吊销（revoked）的 key 不会被配置文件重新启用；需 `key rotate` 或新建用户。
- 配置文件中缺失的额度字段会按“不限”同步，等同于清空该 key 原有额度。
- 文件用户只更新 key/额度/权限，不覆盖模型价格；模型价格仍由 `price` 命令管理。
- 文件修改后需要重启服务生效。

### 上游 key 的安全

为了支持纯文件配置，`upstream_api_key` 可以直接写在 JSON/TOML 中；但这样文件本身就是敏感文件。优先级：

```text
环境变量 TR_UPSTREAM_API_KEY
  > upstream_api_key_env 指定的环境变量（默认 TOKEN_ROUTER_UPSTREAM_KEY）
  > 配置文件中的 upstream_api_key
```

Linux 一键部署仍采用 systemd EnvironmentFile（`0600`）保存上游 key，推荐生产使用该方式。若把 key 写入配置文件，请至少执行 `chmod 600 config.json` 并避免提交到 Git。

## Linux 一键部署（systemd）

仓库根目录的 [deploy.sh](deploy.sh) 面向 systemd Linux，要求 root/sudo、Python 3.10+ 和 systemd。它不需要 pip、虚拟环境或任何第三方 Python 包。

```bash
cd /path/to/TokenRouter
chmod +x deploy.sh
sudo ./deploy.sh --upstream-base-url https://api.openai.com/v1 --port 8787
```

脚本会：

1. 检查 Python 版本和 systemd；
2. 创建系统用户/组 `token-router`；
3. 将 `token_router/` 安装到 `/opt/token-router`；
4. 生成 `/etc/token-router/config.json`（不含 key）；
5. 安全提示输入上游 key，写入 `/etc/token-router/token-router.env`，权限 `0600`；
6. 初始化/迁移 `/var/lib/token-router/router.db`；
7. 创建并启动 `token-router.service`，带 `ProtectSystem=strict`、`NoNewPrivileges` 等 systemd 加固；
8. 首次安装时创建名为 `default` 的下游 key，并只在终端显示一次；
9. 安装管理命令 `/usr/local/bin/token-router-admin`。

常用参数：

```bash
sudo ./deploy.sh --help
sudo ./deploy.sh --host 127.0.0.1 --port 8787 --no-key
sudo ./deploy.sh --service-name token-router --data-dir /var/lib/token-router
```

安装完成后：

```bash
# 管理命令必须以 root 运行，内部会切换为服务用户，避免 SQLite 文件属主错乱
sudo token-router-admin status
sudo token-router-admin usage
sudo token-router-admin requests --limit 20
sudo token-router-admin key list

# 配置价格与限额
sudo token-router-admin key update 1 --price-in 0.50 --price-out 1.50 --daily-tokens 2m
sudo token-router-admin price set --model gpt-4o-mini --input 0.15 --output 0.60 --cached 0.075

# 服务管理
sudo systemctl status token-router
sudo journalctl -u token-router -f
```

- 脚本可重复执行用于升级：保留现有配置、上游 key 和数据库；`--force-config` 会重新生成配置。
- 部署后可编辑 `/etc/token-router/config.json` 增加 `users` 和模型权限，然后 `systemctl restart token-router` 同步；也可以继续用 `token-router-admin` 管理。
- 如果已有 `TOKEN_ROUTER_UPSTREAM_KEY` 环境变量，可非交互传入；否则脚本会安全提示输入。
- 默认只监听 `127.0.0.1`。绑定 `0.0.0.0` 前请在前面放 TLS 反向代理和防火墙，因为网关本身不终止 TLS。
- 卸载：先备份数据，然后 `systemctl disable --now token-router`，删除服务单元、`/usr/local/bin/token-router-admin`、`/opt/token-router`，并按需删除 `/etc/token-router` 与 `/var/lib/token-router`。

## 支持的端点

| 方法 | 路径 | 是否计费 | 说明 |
|---|---|---|---|
| `POST` | `/v1/chat/completions` | 是 | 支持非流式与 SSE |
| `POST` | `/v1/completions` | 是 | 支持非流式与 SSE |
| `POST` | `/v1/embeddings` | 是 | 输出 token 按 0 计 |
| `POST` | `/v1/responses` | 是 | 支持 Responses API usage / SSE 事件 |
| `GET`/`HEAD` | `/v1/models`、`/v1/models/{id}` | 否 | 代理查询模型 |
| `GET`/`HEAD` | `/health` | 否 | 本地健康检查，无需鉴权 |

其他路径返回 404，避免通过未识别别名绕过计费。本项目面向 OpenAI 兼容上游；Anthropic 原生协议、图片/音频计费没有实现。

## 配置

主配置可以是 [config.json](config.json) 或 `.toml` 文件，相对路径以配置文件所在目录为基准。首次 `serve` 找不到默认 `config.json` 时会自动生成；显式指定 `.toml` 路径时会生成 TOML。常用字段：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `host` | `127.0.0.1` | 监听地址；默认仅本机 |
| `port` | `8787` | 唯一接入端口 |
| `upstream_base_url` | `https://api.openai.com/v1` | 上游基础地址，可带 `/v1` 或自定义前缀 |
| `upstream_api_key` | `""` | 可选；直接写上游 key（不推荐提交 Git） |
| `upstream_api_key_env` | `TOKEN_ROUTER_UPSTREAM_KEY` | 上游 key 的环境变量名 |
| `upstream_auth_header` | `Authorization` | 上游鉴权头；兼容 `X-Api-Key` 风格服务 |
| `upstream_auth_scheme` | `"Bearer "` | 鉴权值前缀；为空则直接放 key |
| `upstream_headers` | `{}` | 附加到上游请求的固定头（如组织 ID），不能放鉴权头 |
| `data_dir` | `data` | SQLite 数据目录 |
| `database_file` | `router.db` | 数据库文件名 |
| `timezone_offset_minutes` | `480` | 计费时区，UTC+08:00 即 480 |
| `currency` / `currency_symbol` | `USD` / `$` | 仅展示与配置语义；不接入汇率 |
| `default_price_in_micros_per_mtok` | `0` | 新建 key 的默认输入价（微单位/百万 token） |
| `default_price_out_micros_per_mtok` | `0` | 新建 key 的默认输出价 |
| `default_max_output_tokens` | `4096` | 请求未指定最大输出时的预占 token 数 |
| `max_request_bytes` | 16 MiB | 请求体上限 |
| `max_response_bytes` | 64 MiB | 非流式响应上限/流式总长度保护 |
| `max_concurrency` | 128 | 并发请求上限，超出返回 503 |
| `connect_timeout_seconds` | 10 | 上游连接超时 |
| `request_timeout_seconds` | 300 | 上游读写超时 |
| `upstream_pool_size` | 32 | 上游 keep-alive 空闲连接池大小 |
| `inject_stream_usage` | `true` | 自动请求流式 usage |
| `verify_upstream_tls` | `true` | 校验证书；生产环境不要关闭 |
| `log_level` | `INFO` | `DEBUG`/`INFO`/`WARNING`/`ERROR` |
| `users` | `[]` | 文件声明的用户列表；启动时同步，字段见“文件配置” |

环境变量覆盖：`TR_CONFIG`、`TR_HOST`、`TR_PORT`、`TR_DATA_DIR`、`TR_UPSTREAM_BASE_URL`、`TR_TIMEZONE_OFFSET_MINUTES`、`TR_LOG_LEVEL`、`TR_UPSTREAM_API_KEY`。

模型级价格不放在配置文件中，而是保存在 SQLite 的 `model_prices` 表中，通过 `price set/list/remove` 管理，修改后立即生效。

### 上游地址映射

`upstream_base_url` 的业务路径按以下规则映射：

| 配置 | 下游路径 | 上游路径 |
|---|---|---|
| `https://api.openai.com` | `/v1/chat/completions` | `/v1/chat/completions` |
| `https://api.openai.com/v1` | `/v1/chat/completions` | `/v1/chat/completions` |
| `https://gateway.example.com/openai/v1` | `/v1/chat/completions` | `/openai/v1/chat/completions` |
| `http://127.0.0.1:11434/v1` | `/v1/chat/completions` | `/v1/chat/completions` |

因此 OpenAI、DeepSeek、OpenRouter、vLLM/Ollama 的 OpenAI 兼容接口通常只需改 `upstream_base_url` 和对应环境变量。

## 管理命令

所有管理操作都是**本地 CLI**，直接读写 SQLite，因此不会多开管理端口，服务器运行中也可以执行。

```powershell
.\run.cmd --help
.\run.cmd key --help
```

### key 管理

```powershell
# 列出 key（默认不含已吊销）
.\run.cmd key list
.\run.cmd key list --all

# 查看配置和当前日/月用量、剩余额度
.\run.cmd key show 1
.\run.cmd key show tr_AbCdEf

# 修改限额/价格；none 表示取消限额
.\run.cmd key update 1 --daily-tokens 5m --monthly-cost 200
.\run.cmd key update 1 --daily-tokens none

# 临时停用/启用；吊销不可逆
.\run.cmd key disable 1
.\run.cmd key enable 1
.\run.cmd key revoke 1 --yes

# 轮换：旧 key 立即失效，新 key 只显示一次
.\run.cmd key rotate 1 --yes
```

`key` 参数可以使用数字 ID 或 key 前缀。前缀匹配多个 key 时会要求改用 ID。

### 模型权限

```powershell
# 创建时直接限制
.\run.cmd key create --name "team-a" --allowed-model "gpt-4o-mini" --allowed-model "gpt-4o*"

# 后续修改
.\run.cmd key models show 1
.\run.cmd key models set 1 --model gpt-4o-mini --model "o3*"
.\run.cmd key models set 1 --all       # 取消限制
.\run.cmd key models set 1 --none      # 禁止所有模型
```

模型权限支持 `fnmatch` 通配（大小写敏感）。`--all` 等价于数据库中的 `NULL`，`--none` 等价于空列表。

### 模型价格

模型价格支持三级回退：

```text
key 的模型覆盖价 → 全局模型价 → key 默认输入/输出价
```

```powershell
# 全局价格：所有 key 的该模型默认使用此价格
.\run.cmd price set --model gpt-4o-mini --input 0.15 --output 0.60 --cached 0.075

# 指定 key 的覆盖价：优先级高于全局价
.\run.cmd price set --key 1 --model gpt-4o-mini --input 0.20 --output 0.80 --cached 0.10

# 不写 --cached 时，缓存命中价默认等于该模型的输入价
.\run.cmd price set --model gpt-4o --input 2.50 --output 10 --cached 1.25

# 查看全局价；带 --key 时同时查看该 key 的覆盖价
.\run.cmd price list
.\run.cmd price list --key 1

# 删除覆盖价/全局价
.\run.cmd price remove --key 1 --model gpt-4o-mini --yes
.\run.cmd price remove --model gpt-4o-mini --yes
```

模型名必须与下游请求中的 `model` 字段**精确匹配**（不区分前后空格，但不支持通配符）。未匹配到模型价时使用 key 的默认价格，缓存命中部分也按输入价计费。

### 查看用量与账本

```powershell
# 今日/本月按 key 汇总，含缓存命中、限额与剩余 token
.\run.cmd usage

# 只看某个 key；包含已吊销 key 需加 --all
.\run.cmd usage --key 1
.\run.cmd usage --all

# 最近 50 条请求明细
.\run.cmd requests --limit 50
.\run.cmd requests --key 1 --limit 20

# 机器可读输出
.\run.cmd usage --json
.\run.cmd requests --json
.\run.cmd status --json

# 运行配置（不会显示上游 key 明文）
.\run.cmd show-config
```

### 清理请求明细

```powershell
# 删除 90 天前的请求明细，保留日/月计费汇总
.\run.cmd prune --days 90 --yes
```

## 计费模型

价格按每 **100 万 token** 配置，分为三个维度：

- 输入价 `input`：普通输入 token；
- 输出价 `output`：输出 token；
- 缓存命中价 `cached`：输入 token 中命中上游 prompt cache 的部分，未配置时默认等于输入价。

价格来源优先级：

```text
key 的模型覆盖价 > 全局模型价 > key 默认输入/输出价
```

key 的默认价格在 `key create/update --price-in/--price-out` 设置；模型价格通过 `price set` 设置。

内部金额使用**整数微单位**（`1 单位 = 1,000,000 微单位`），避免浮点误差。单次请求费用向上取整到 1 微单位：

```text
uncached = max(0, input_tokens - cached_input_tokens)
cost = ceil(
  (uncached × input_price
   + cached_input_tokens × cached_price
   + output_tokens × output_price) / 1_000_000
)
```

例如输入 100 token，其中 60 token 命中缓存，输出 10 token；输入价 `$2/M`、缓存价 `$0.5/M`、输出价 `$10/M`：

```text
40 × $2/M + 60 × $0.5/M + 10 × $10/M
= $0.00008 + $0.00003 + $0.0001
= $0.00021（210 微单位）
```

计费规则：

1. 仅上游返回 **2xx** 时计费；上游 4xx/5xx、网络异常、配额拒绝均不计费。
2. 有 `usage` 时以 provider 返回的 token 为准，`estimated=0`：
   - chat/completions：`prompt_tokens`、`completion_tokens`；
   - Responses API：`input_tokens`、`output_tokens`；
   - 缓存命中：`prompt_tokens_details.cached_tokens`、`input_tokens_details.cached_tokens`、DeepSeek 的 `prompt_cache_hit_tokens`，以及 `cache_read_input_tokens` 兼容字段。
3. 无 usage 时使用本地估算，`estimated=1`：
   - 输入：遍历 messages / prompt / input / tools 等文本做估算；图片按固定额度近似；
   - 输出：从非流式响应文本或 SSE delta 文本估算；
   - 估算模式下无法知道缓存命中，缓存 token 记为 0，全部输入按普通输入价计费；
   - 流式客户端中途断开时，按已收到的部分估算。
4. `/v1/models` 和 `/health` 不计费；模型查询仍会在请求账本中留下状态记录。
5. token 限额按**总输入 + 输出**计算（缓存命中 token 也是输入 token 的一部分）；费用限额按上面公式计算出的费用。
6. 请求账本保存 `input_tokens`、`cached_input_tokens`、`output_tokens`、`cost_micros`；日/月汇总也保存缓存命中量。

> 缓存命中 token 必须由上游 usage 提供；如果上游不返回缓存字段，路由器不会猜测，按普通输入计费。本地估算是为了在兼容性较差的上游上仍能收费和限流，不是精确 tokenizer。

## 配额与并发语义

每个 key 可以同时拥有四个限额：

- `daily_token_limit`
- `monthly_token_limit`
- `daily_cost_limit_micros`
- `monthly_cost_limit_micros`

**转发前**会预占：

```text
预占 token = 估算输入 token + 请求的 max_tokens/max_completion_tokens/max_output_tokens
                            （未指定时用 default_max_output_tokens）
预占费用 = 按解析出的模型价格计算；输入预占使用 max(输入价, 缓存命中价)
```

预占时还不知道缓存命中比例，因此输入部分按“输入价/缓存价较高者”保守计算；结算时再按实际 usage 的缓存 token 精确计费。

检查同时覆盖“已落账用量 + 当前在途预占 + 本次预占”。因此并发请求不会一起穿透配额。预算不足时直接返回 `429`：

```json
{
  "error": {
    "message": "Daily token quota exceeded for this API key",
    "type": "insufficient_quota",
    "code": "quota_exceeded",
    "details": {
      "period": "day",
      "kind": "tokens",
      "limit": 2000000,
      "used": 1999000,
      "reserved": 4000,
      "requested": 3000,
      "reset_at": 1761148800,
      "retry_after_seconds": 3600
    }
  }
}
```

响应同时带 `Retry-After` 头。配额按 `timezone_offset_minutes` 的本地日/月边界自动重置，默认 UTC+08:00 的每日 00:00 和每月 1 日 00:00。

### 为什么默认会预占 4096 输出 token？

如果请求没有传 `max_tokens`，路由器无法知道模型会生成多长。为保证并发下不超卖配额，它使用 `default_max_output_tokens` 作为预占值。

如果某个 key 的日限额很小，可能出现“额度明明还剩一些却被 429”的情况。两种处理方式：

1. 调小 `config.json` 中的 `default_max_output_tokens`；
2. 要求下游请求携带 `max_tokens`（推荐，预占更准确）。

### 单请求超限

预占是准入控制，不会在流式输出中途强制截断。如果上游实际输出超过预占值，路由器会按实际 usage 落账，该次请求可能略微超过限额，后续请求会被阻止，直到配额重置。这是为了避免破坏 SSE 响应和下游体验。

### 单进程约束

在途预占保存在进程内存中，因此配额并发控制假定**同一个数据库只由一个 `serve` 进程服务**。不要同时启动多个进程监听同一数据库；如需水平扩展，需要把预占迁移到数据库/Redis 等共享存储。

## 流式计费细节

- 路由器请求上游 `Accept-Encoding: identity`，以减少无法解析压缩响应的情况。
- 对 `stream=true` 的 chat/completions，自动把 `stream_options.include_usage` 设为 `true`，并保留客户端传入的其他 `stream_options`。
- SSE 字节原样转发给下游，同时用增量解析器提取 usage 和输出文本；不会把整个流缓存到内存。
- 若上游不支持 `stream_options` 并因此报错，可将 `inject_stream_usage` 设为 `false`，此时流式请求会使用本地估算计费。
- 客户端中途断开时，路由器会关闭上游连接，并按已接收内容估算费用；请求账本状态仍为 200，并在错误列记录 client disconnected。
- Responses API 的 usage 从 `response.completed` 事件中提取。

## 数据与安全

- 数据库：`data/router.db`（首次运行创建），SQLite WAL 模式。
- 表：
  - `api_keys`：虚拟 key 哈希、前缀、默认价格、限额、模型权限、状态；
  - `model_prices`：全局模型价和按 key 覆盖价（`key_id` 为 NULL 表示全局）；
  - `usage_buckets`：按 key + 日/月 + 周期起点聚合的输入/缓存/输出 token、请求数和费用；
  - `request_log`：请求级明细（含缓存命中 token），可用 `prune` 清理。
- 数据库 schema 版本为 3；从 v1/v2 启动时会自动补充缓存列、`model_prices` 表和 `allowed_models` 列，不丢失历史数据。
- 上游 key 可来自环境变量或配置文件；Linux systemd 部署使用 `0600` 的 EnvironmentFile，推荐生产使用。
- 配置文件中的 `users[].api_key` 是明文下游 key；数据库只保存 SHA-256 哈希。若配置文件包含上游 key 或用户 key，请使用 `chmod 600` 并避免提交到 Git。
- 下游明文 key 只在创建/轮换时输出一次；数据库只有 SHA-256 哈希。
- 默认绑定回环地址。绑定 `0.0.0.0` 会打印警告；本机端口没有 TLS，不建议直接暴露到局域网/公网。需要远程访问时，建议在前面加带 TLS 的反向代理和防火墙。
- 请求日志不保存 prompt/response 正文，但会保存模型、端点、输入/缓存/输出 token、费用、延迟、客户端 IP 和错误摘要；请保护 `data/` 目录并按需 `prune`。
- 没有内置 RPM/TPM 限速，只有日/月配额；需要更细的速率限制时可在反向代理层实现。
- 本项目不提供预付费钱包/实际扣款；“计费”指按 token 计算费用、记录账本并执行费用限额。若需要余额扣减，可在 `usage_buckets`/`request_log` 上扩展。

## 运维

```powershell
# 总体状态
.\run.cmd status

# 今日/本月明细
.\run.cmd usage

# 最近请求
.\run.cmd requests --limit 20

# 清理 90 天前的请求明细
.\run.cmd prune --days 90 --yes
```

日志输出到启动终端，默认 `INFO` 级别，一行一个请求，包含请求 ID、key 名称、端点、状态、输入/缓存/输出 token、费用、是否估算、延迟和客户端 IP；不记录 key 明文和请求正文。

备份时建议停服后复制 `data/` 下全部文件，或使用 SQLite 官方 `.backup` 命令做在线备份。修改上游 key 后重启 `serve` 即可。

## 项目结构

```text
TokenRouter/
├─ config.json               # 默认 JSON 配置
├─ config.example.toml       # 带 users 的 TOML 示例
├─ run.cmd                   # Windows 下使用指定 Python 环境的启动器
├─ deploy.sh                 # Linux systemd 一键部署/升级脚本
├─ token_router/
│  ├─ __main__.py            # python -m token_router
│  ├─ cli.py                 # init/serve/key/price/usage/requests/prune 等
│  ├─ config.py              # JSON/TOML 加载、自动生成、用户配置、校验
│  ├─ config_users.py        # 启动时把文件用户同步到 SQLite
│  ├─ db.py                  # SQLite schema、key、模型权限/价格、用量桶、账本
│  ├─ quota.py               # 并发预占、配额检查、结算
│  ├─ proxy.py               # 单端口 HTTP/SSE 代理、鉴权、模型权限
│  ├─ upstream.py            # 上游 HTTP 客户端与 keep-alive 连接池
│  ├─ usage.py               # usage/缓存字段提取、注入、SSE 解析、token 估算
│  └─ util.py                # 金额/周期/表格等工具
└─ tests/                    # unittest 单元与集成测试
```

## 测试

```powershell
cd E:\TokenRouter
& 'F:\uVenvs\ML_venv\Scripts\python.exe' -m unittest discover -s tests -v
```

测试不依赖 pytest 或任何第三方包，覆盖：

- 配额预占、并发超限、结算释放；
- 日/月用量桶、缓存 token、请求账本、清理；
- 模型价格全局/按 key 覆盖、价格优先级、缓存价计费；
- JSON/TOML 文件配置、首次启动自动生成、用户同步；
- 每 key 模型权限（精确/通配/禁止全部）；
- SQLite v1/v2 → v3 自动迁移；
- 无效 key、停用/吊销/轮换；
- 多下游 key 共用一个上游 key；
- 非流式 usage 计费、上游错误不计费、无 usage 兜底估算；
- SSE 流式 usage 注入、分块解析、无 usage 兜底；
- 分块请求体、健康检查、模型查询、上游 keep-alive 复用；
- 配置解析、上游路径映射、金额和周期计算。

## 已知限制

- 仅支持 OpenAI 兼容协议；不原生支持 Anthropic Messages API。
- 没有图片、音频、视频等非 token 计费模型。
- 模型价格按模型 ID 精确匹配，不支持通配符/正则；模型权限支持 `fnmatch` 通配。
- 模型权限在生成类请求转发前检查；`/v1/models` 列表仍返回上游全部模型，不按 key 过滤。
- 缓存命中 token 依赖上游 usage 字段；上游不返回时按普通输入价计费。
- 本地估算不是精确 tokenizer；精确金额依赖上游 usage。
- 配额时区使用固定 UTC 偏移，不支持带夏令时的 IANA 时区（默认 UTC+08:00 不受影响）。
- 配额预占是单进程内存状态，不支持多实例共享。
- 配置文件用户只在服务启动时同步；修改后需要重启。文件中删除用户不会自动删除/停用数据库中的 key。
- 管理面只有 CLI；没有 Web UI，也不会额外开放管理端口。
- 没有内置预付费余额/充值。
- Linux 一键部署假定 systemd 和系统 Python 3.10+；使用 TOML 配置需要 Python 3.11+（`tomllib`）；JSON 配置在 Python 3.10+ 可用。

## 版本管理

仓库使用 Git。典型工作流：

```powershell
git status
git add .
git commit -m "feat: ..."
git log --oneline
```

数据库、日志和本地临时文件已在 `.gitignore` 中排除。若在配置文件中写入上游 key 或用户 key，请不要提交该文件，或先将其移到安全的本地配置文件。
