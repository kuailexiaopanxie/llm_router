# Coding LLM Router v0.1 实施附录

> 本文是 [v0.1 设计规格](./coding-llm-router-v0.1-spec.md) 的实施细节。规格中的产品目标和协议语义优先于本附录。

## 1. 配置

v0.1 使用单个 `router.yaml` 作为唯一配置源。密钥只能通过环境变量引用。

```yaml
version: 1

server:
  host: 127.0.0.1
  port: 8848
  client_api_key_env: LLM_ROUTER_CLIENT_API_KEY
  max_request_bytes: 16777216

storage:
  sqlite_path: ~/.llm-router/router.db

providers:
  anthropic:
    type: anthropic
    base_url: https://api.anthropic.com
    api_key_env: LLM_ROUTER_ANTHROPIC_API_KEY
    auth_scheme: x_api_key
    max_concurrency: 16

models:
  fast:
    provider: anthropic
    upstream_model: configured-fast-model
    tier: fast
    capabilities: [streaming, tools, prompt_cache]
    max_input_tokens: 200000
    input_price_per_million: 0
    output_price_per_million: 0
  balanced:
    provider: anthropic
    upstream_model: configured-balanced-model
    tier: balanced
    capabilities: [streaming, tools, thinking, vision, prompt_cache]
    max_input_tokens: 200000
    input_price_per_million: 0
    output_price_per_million: 0
  deep:
    provider: anthropic
    upstream_model: configured-deep-model
    tier: deep
    capabilities: [streaming, tools, thinking, vision, prompt_cache]
    max_input_tokens: 200000
    input_price_per_million: 0
    output_price_per_million: 0

profiles:
  code/auto:
    mode: auto
  code/fast:
    primary: fast
    fallback: [balanced]
  code/balanced:
    primary: balanced
    fallback: [deep]
  code/deep:
    primary: deep
    fallback: []

routing:
  default_profile: code/auto
  policy_version: v1
  session_ttl_seconds: 7200
  failure_escalation_requests: 2
  fast_max_input_tokens: 8000
  balanced_max_input_tokens: 64000

timeouts:
  connect_seconds: 5
  response_header_seconds: 30
  non_stream_deadline_seconds: 120
  stream_idle_seconds: 90
  stream_max_seconds: 1800
```

配置规则：

- 启动时执行 schema、引用完整性、能力和 fallback 环检查。
- 配置无效时 fail fast，不启动监听端口。
- Pydantic 模型使用 `extra="forbid"`，避免拼写错误被忽略。
- 不允许在 YAML 中直接写 API key。
- v0.1 不做文件热加载，配置变更通过重启生效。
- 实际生效配置生成稳定 hash，作为 policy version 的一部分。
- fallback 必须引用已声明模型，不能引用自身或形成环。
- 价格缺失时成本记录为 `unknown`，不伪造零成本；示例中的零值仅表示待填配置。

## 2. 数据对象

### 2.1 Protocol Envelope

```text
request_id
protocol = anthropic_messages
raw_body
safe_headers
stream
received_at
```

`raw_body` 只存在于请求生命周期内。Gateway 只允许修改经过明确规定的 model 和认证信息。

### 2.2 Routing Request

```text
requested_profile
required_capabilities
estimated_input_tokens
tool_rounds
task_signals
outcome_signals
session_id?
stream
```

### 2.3 Execution Plan

```text
primary: ModelTarget
fallbacks: list[ModelTarget]
attempt_limit
timeouts
route_reason
auxiliary_reasons
profile
policy_version
```

Execution Plan 在进入 Execution Engine 后不可变。

### 2.4 Route Event

```text
request_id
received_at
protocol
profile
stream
feature_summary
primary_model
final_model
route_reason
policy_version
status
attempt_count
time_to_first_event_ms?
total_latency_ms
input_tokens?
output_tokens?
estimated_cost?
error_code?
```

## 3. HTTP 与 header 处理

允许从客户端转发到 Anthropic 的 header：

- `anthropic-version`
- `anthropic-beta`
- `content-type`
- `accept`
- 明确列入配置的供应商扩展 header

默认删除：

- `authorization`
- `x-api-key`
- `host`
- `content-length`
- `connection`
- `keep-alive`
- `transfer-encoding`
- `te`
- `trailer`
- `upgrade`
- `cookie`

响应只复制 Content-Type、允许的供应商 request ID 和协议所需 header。hop-by-hop header 不向客户端返回。

`count_tokens` 请求使用同一 Routing Kernel，但标记为 `count_only`，不创建模型生成 attempt，也不改变 session 失败状态。

## 4. 错误码映射

| 内部错误码 | HTTP | Anthropic error type | 说明 |
|---|---:|---|---|
| `router_invalid_request` | 400 | `invalid_request_error` | JSON、字段或 header 无效 |
| `router_unknown_model` | 400 | `invalid_request_error` | 未声明的 profile 或直连别名 |
| `router_unauthorized` | 401 | `authentication_error` | 本地 token 无效 |
| `router_no_capable_model` | 422 | `invalid_request_error` | 没有满足能力和容量的模型 |
| `router_upstream_rejected` | 502 | `api_error` | 上游拒绝且不可 fallback |
| `router_upstream_exhausted` | 503 | `overloaded_error` | 所有计划内 attempt 耗尽 |
| `router_timeout` | 504 | `api_error` | 超过执行 deadline |
| `router_cancelled` | 499 | `api_error` | 客户端主动取消 |
| `router_not_ready` | 503 | `api_error` | 进程未就绪 |

上游错误 message 只保留规范化摘要。供应商 request ID 可以进入结构化事件，但不能把完整响应正文返回给客户端。

## 5. 持久化与观测

SQLite 使用 WAL 模式和后台有界写队列。

### 5.1 `route_requests`

- request ID、时间、协议、profile、stream。
- Task Context 的脱敏特征。
- primary、最终模型、route reason、policy version。
- 状态、首 event 延迟、总延迟、输入/输出 token、估算成本。
- attempt 数量和规范化错误码。

### 5.2 `route_attempts`

- request ID、attempt 序号、provider、model。
- 开始时间、持续时间、状态、HTTP 状态和规范化错误码。

### 5.3 禁止持久化

- 消息正文和 system prompt。
- tool 参数和 tool result 原文。
- 模型响应正文。
- API key、Authorization header 或 Cookie。

### 5.4 指标

至少提供：请求量、成功率、按 profile/tier/model 的路由量、attempt/fallback 次数、routing latency、首 event 延迟、总延迟、token、成本、活跃 stream、取消、idle timeout 和 telemetry 队列丢弃量。

指标 label 只能来自配置中的有界枚举，不能使用 request ID、session ID 或错误 message。

### 5.5 日志

- 使用结构化 JSON 日志。
- 所有日志 message 使用英文。
- 日志包含 request ID、模块、event、状态和规范化错误码。
- 默认不打印 request body、response body 或 header 全量内容。
- 调试模式仍必须执行密钥和认证 header 脱敏。

Telemetry 写入失败只增加内部计数；非关键观测丢弃，主请求继续完成。

## 6. 并发与生命周期

- HTTP 和供应商调用使用 async I/O。
- 每个 provider 使用有界 semaphore 限制并发。
- SQLite 写入通过有界后台队列串行化。
- shutdown 时停止接收新请求，等待或取消活跃请求至宽限期结束。
- shutdown 尝试 drain telemetry 队列，超过宽限期后退出。
- 同步第三方调用必须进入有界线程池，不得阻塞事件循环。

## 7. 技术选型与代码结构

技术栈：

- Python 3.12
- FastAPI + Uvicorn
- Pydantic v2
- httpx
- PyYAML
- aiosqlite
- prometheus-client
- 可选 OpenTelemetry
- pytest、Ruff、mypy

v0.1 不使用 Anthropic SDK，直接基于 httpx 保留未知字段和 SSE event；不使用 LiteLLM，因为当前只有一个生产 Provider Adapter。

```text
src/llm_router/
├── app.py
├── config.py
├── domain.py
├── gateway/
│   ├── auth.py
│   ├── errors.py
│   └── anthropic.py
├── routing/
│   ├── kernel.py
│   ├── features.py
│   ├── policy.py
│   └── session.py
├── execution/
│   ├── engine.py
│   ├── failure.py
│   └── streaming.py
├── providers/
│   ├── port.py
│   └── anthropic.py
└── telemetry/
    ├── recorder.py
    ├── sqlite_store.py
    └── metrics.py
```

约束：

- `app.py` 只负责装配依赖和生命周期，不放路由业务逻辑。
- 供应商类型不得出现在 Routing Kernel 的规则实现中。
- 单文件或单函数接近 500 行时按语义拆分。
- 公共函数具有简洁的函数级注释或 docstring。

## 8. 验证策略

验证只通过模块 interface 和 HTTP interface 观察行为，不依赖内部调用顺序。

### 8.1 协议 fixture

覆盖非流式文本、流式文本和 usage、system blocks、tool definitions/tool use/tool result、thinking delta、vision、prompt cache、未知字段、未知 SSE event 和客户端取消。

除明确允许改写的 model 和认证信息外，上游收到的请求必须保持客户端语义。

### 8.2 路由表验证

使用表驱动用例验证所有虚拟 profile、硬能力过滤、token 容量过滤、失败升级和衰减、不确定请求回到 balanced，以及无合格模型时明确失败。

### 8.3 fallback 验证

Fake Provider Adapter 模拟连接失败、429/529、首 event 前断流、首 event 后断流、能力不足和客户端取消。必须证明 Commit Point 前可 fallback，Commit Point 后绝不重复生成。

### 8.4 端到端验证

- 本地 fake upstream 覆盖自动化场景。
- 真实 Anthropic key 的验证必须显式启用，默认不运行。
- 至少完成一次 Claude Code 文本问答和一次 tool loop 人工验收。

## 9. 性能目标

在本机 fake upstream 环境下：

- Routing Kernel `plan()` p95 小于 5 ms。
- 不含上游耗时的代理首 event 开销 p95 小于 20 ms。
- 支持至少 50 个并发 SSE 连接且事件不串流。
- telemetry 或 SQLite 暂时不可用不导致主请求失败。
- 进程空闲内存目标小于 200 MiB。

这些是 v0.1 工程目标，不构成公网部署 SLA。

## 10. 里程碑

### M0：规格与工程骨架

冻结 interface、配置 schema 和错误码，建立 Python 包、格式化、静态检查和应用入口。

### M1：透明代理

完成认证、`/v1/messages`、count tokens、JSON、SSE、取消、超时和协议 fixture。

### M2：确定性路由

完成 model registry、虚拟 profile、能力过滤、route reason 和 session 失败升级。

### M3：执行可靠性

完成 Execution Plan、attempt、fallback、Commit Point、并发限制和优雅退出。

### M4：观测与发布

完成 SQLite、指标、结构化日志、成本估算、Claude Code 人工验收和本地性能验证。
