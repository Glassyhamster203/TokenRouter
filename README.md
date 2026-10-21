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
- **真实上游 key 不落盘**：上游 key 只从环境变量读取；配置文件可安全提交到 Git。
- **虚拟 key**：随机生成、只在创建时显示一次，数据库只保存 SHA-256 哈希和前 12 位前缀。
- **请求级计费**：记录每个请求的模型、端点、输入/输出 token、费用、延迟、状态和是否估算。
- **日/月双重限额**：每个 key 可独立设置每日、每月 token 限额和费用限额；两类限额可同时生效。
- **并发安全预占**：转发前预占 token/费用预算，避免多个并发请求同时穿过配额。429 响应带 `Retry-After`。
- **流式支持**：原样转发 SSE，同时从流中提取 `usage`；对 OpenAI chat/completions 自动注入 `stream_options.include_usage=true`。
- **精确优先、估算兜底**：优先使用上游返回的 usage；没有 usage 时用无分词器的保守估算，并在账本中标记 `estimated=1`。
- **零第三方依赖**：只使用 Python 标准库（`http.server`、`http.client`、`sqlite3`、`ssl` 等）；测试使用 `unittest`。
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

需要持久化时可以用 `setx`，但它只对新开的终端生效。不要把真实 key 写进 `config.json`、脚本或 Git；配置加载器会拒绝 `upstream_api_key` 字段。

### 3. 创建下游 key

```powershell
.\run.cmd key create --name "team-a" `
  --daily-tokens 2m --monthly-tokens 30m `
  --daily-cost 5 --monthly-cost 80 `
  --price-in 0.50 --price-out 1.50
```

- `--daily-tokens` / `--monthly-tokens` 支持 `100k`、`1.5m`、`2b`，`none` 表示不限。
- `--daily-cost` / `--monthly-cost` 的单位由 `config.json` 的 `currency` 决定；`none` 表示不限。
- `--price-in` / `--price-out` 表示**每 100 万 token** 的价格；不填则使用配置中的默认价。
- 明文 key 只显示一次，请立即保存。

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

主配置在 [config.json](config.json)。相对路径以配置文件所在目录为基准。常用字段：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `host` | `127.0.0.1` | 监听地址；默认仅本机 |
| `port` | `8787` | 唯一接入端口 |
| `upstream_base_url` | `https://api.openai.com/v1` | 上游基础地址，可带 `/v1` 或自定义前缀 |
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

环境变量覆盖：`TR_CONFIG`、`TR_HOST`、`TR_PORT`、`TR_DATA_DIR`、`TR_UPSTREAM_BASE_URL`、`TR_TIMEZONE_OFFSET_MINUTES`、`TR_LOG_LEVEL`、`TR_UPSTREAM_API_KEY`。

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

### 查看用量与账本

```powershell
# 今日/本月按 key 汇总，含限额与剩余 token
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

每个 key 有两个价格：

- `price_in_micros_per_mtok`：输入 token 每 100 万 token 的价格；
- `price_out_micros_per_mtok`：输出 token 每 100 万 token 的价格。

内部金额使用**整数微单位**（`1 单位 = 1,000,000 微单位`），避免浮点误差。单次请求费用向上取整到 1 微单位：

```text
cost = ceil((input_tokens × price_in + output_tokens × price_out) / 1_000_000)
```

例如输入 10 token、输出 5 token，输入价 `$1/M`、输出价 `$2/M`：

```text
(10×1,000,000 + 5×2,000,000) / 1,000,000 = 20 微单位 = $0.00002
```

计费规则：

1. 仅上游返回 **2xx** 时计费；上游 4xx/5xx、网络异常、配额拒绝均不计费。
2. 有 `usage` 时以 `prompt_tokens`/`completion_tokens` 或 Responses API 的 `input_tokens`/`output_tokens` 为准，`estimated=0`。
3. 无 usage 时使用本地估算，`estimated=1`：
   - 输入：遍历 messages / prompt / input / tools 等文本做估算；图片按固定额度近似；
   - 输出：从非流式响应文本或 SSE delta 文本估算；
   - 流式客户端中途断开时，按已收到的部分估算。
4. `/v1/models` 和 `/health` 不计费；模型查询仍会在请求账本中留下状态记录。
5. 费用限额基于计算出的费用；token 限额基于输入 token + 输出 token。

> 本地估算是为了在兼容性较差的上游上仍能收费和限流，不是精确 tokenizer。对账要求高时，应优先让上游返回 usage。

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
预占费用 = 按预占 token 和该 key 的价格计算
```

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
  - `api_keys`：虚拟 key 哈希、前缀、价格、限额、状态；
  - `usage_buckets`：按 key + 日/月 + 周期起点聚合的 token/费用；
  - `request_log`：请求级明细，可用 `prune` 清理。
- 上游 key 只存在环境变量中；`config.json` 不含真实 key。
- 下游明文 key 只在创建/轮换时输出一次；数据库只有 SHA-256 哈希。
- 默认绑定回环地址。绑定 `0.0.0.0` 会打印警告；本机端口没有 TLS，不建议直接暴露到局域网/公网。需要远程访问时，建议在前面加带 TLS 的反向代理和防火墙。
- 请求日志不保存 prompt/response 正文，但会保存模型、端点、token、费用、延迟、客户端 IP 和错误摘要；请保护 `data/` 目录并按需 `prune`。
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

日志输出到启动终端，默认 `INFO` 级别，一行一个请求，包含请求 ID、key 名称、端点、状态、输入/输出 token、费用、是否估算、延迟和客户端 IP；不记录 key 明文和请求正文。

备份时建议停服后复制 `data/` 下全部文件，或使用 SQLite 官方 `.backup` 命令做在线备份。修改上游 key 后重启 `serve` 即可。

## 项目结构

```text
TokenRouter/
├─ config.json               # 可提交的配置（不含上游 key）
├─ run.cmd                   # 使用指定 Python 环境的启动器
├─ token_router/
│  ├─ __main__.py            # python -m token_router
│  ├─ cli.py                 # init/serve/key/usage/requests/prune 等
│  ├─ config.py              # 配置加载、环境覆盖、校验
│  ├─ db.py                  # SQLite schema、key、用量桶、请求账本
│  ├─ quota.py               # 并发预占、配额检查、结算
│  ├─ proxy.py               # 单端口 HTTP/SSE 代理与鉴权
│  ├─ upstream.py            # 上游 HTTP 客户端与 keep-alive 连接池
│  ├─ usage.py               # usage 提取、注入、SSE 解析、token 估算
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
- 日/月用量桶、请求账本、清理；
- 无效 key、停用/吊销/轮换；
- 多下游 key 共用一个上游 key；
- 非流式 usage 计费、上游错误不计费、无 usage 兜底估算；
- SSE 流式 usage 注入、分块解析、无 usage 兜底；
- 分块请求体、健康检查、模型查询；
- 配置解析、上游路径映射、金额和周期计算。

## 已知限制

- 仅支持 OpenAI 兼容协议；不原生支持 Anthropic Messages API。
- 没有图片、音频、视频等非 token 计费模型。
- 本地估算不是精确 tokenizer；精确金额依赖上游 usage。
- 配额时区使用固定 UTC 偏移，不支持带夏令时的 IANA 时区（默认 UTC+08:00 不受影响）。
- 配额预占是单进程内存状态，不支持多实例共享。
- 管理面只有 CLI；没有 Web UI，也不会额外开放管理端口。
- 没有内置预付费余额/充值。

## 版本管理

仓库使用 Git。典型工作流：

```powershell
git status
git add .
git commit -m "feat: ..."
git log --oneline
```

上游 key、数据库、日志和本地临时文件已在 `.gitignore` 中排除。
