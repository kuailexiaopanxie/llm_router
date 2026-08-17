# Coding LLM Router v0.2 OpenAI Responses 实施附录

> 本文是 [v0.2 OpenAI Responses 设计规格](./coding-llm-router-v0.2-openai-responses-spec.md) 的实施细节。

## 1. 配置变化

示例：

```yaml
providers:
  anthropic:
    type: anthropic
    base_url: https://api.anthropic.com
    api_key_env: LLM_ROUTER_ANTHROPIC_API_KEY
  openai:
    type: openai
    base_url: https://api.openai.com
    api_key_env: LLM_ROUTER_OPENAI_API_KEY
    auth_scheme: bearer
    max_concurrency: 16

models:
  openai_fast:
    provider: openai
    upstream_model: configured-openai-fast-model
    protocol: openai_responses
    state_scope: openai-default
    tier: fast
    capabilities: [streaming, tools, reasoning, vision, structured_output]
    max_input_tokens: 200000
```

配置验证增加：

- OpenAI model 必须引用 `type: openai` provider。
- Model protocol 必须与 provider native protocol 一致。
- stateful target 的 fallback 必须共享 state scope。
- 同一 profile 的协议链不能混用 target。
- auto routing 的每个协议至少需要 fast、balanced、deep 可用 target。

为了让 `code/fast` 等虚拟 profile 同时适用于两个协议，配置中的 target chain 按协议声明：

```yaml
profiles:
  code/fast:
    targets:
      anthropic_messages:
        primary: anthropic_fast
        fallback: [anthropic_balanced]
      openai_responses:
        primary: openai_fast
        fallback: [openai_balanced]
```

v0.1 的旧格式在加载时迁移为只有 `anthropic_messages` 的 target chain。迁移后生成新的 policy hash。

## 2. Telemetry

复用 v0.1 的 `RouteEvent`，增加 bounded 字段：

```text
inbound_protocol
target_protocol
provider_account_scope
response_state_requested
translation_mode = none
```

v0.2 的 `translation_mode` 永远是 `none`，提前固定该字段是为了让未来跨协议版本可以区分同协议和转换调用。

OpenAI usage 从最终 JSON 或 `response.completed` event 提取。若事件不存在 usage，记录 `unknown`，不从正文估算真实计费。

不落盘：input、instructions、output、tool 参数、reasoning、response ID、previous response ID 和 provider key。

## 3. 测试与验收

### 3.1 OpenAI fixture

必须覆盖：

- string input。
- item array input。
- instructions。
- function tools 和 function call output。
- reasoning。
- structured output。
- image input。
- `stream=false` JSON。
- `stream=true` 完整 Responses event 序列。
- `previous_response_id` 和 `store`。
- provider error、rate limit 和 invalid request。
- 未知字段、未知 event 和客户端取消。

### 3.2 Adapter seam

- `RoutingKernel` 测试只使用 protocol、capability 和 sanitized facts，不导入 OpenAI 模块。
- Execution Engine 使用 Fake ProviderPort 和 Fake StreamSemantics，不依赖 OpenAI SDK。
- OpenAI Provider Adapter 测试只验证 URL、认证、model 替换、header 和 raw bytes。
- OpenAI Gateway 测试只验证请求提取、响应/错误渲染和 usage 提取。
- v0.1 Anthropic fixture 必须全部回归通过。

### 3.3 关键断言

1. OpenAI request 永远不会选择 `anthropic_messages` target。
2. Anthropic request 永远不会选择 `openai_responses` target。
3. 不存在 cross-protocol fallback。
4. OpenAI post-commit error 不是 Anthropic SSE event。
5. `previous_response_id` 不会跨 state scope fallback。
6. OpenAI response ID 和 tool call ID 不被改写。
7. Routing Kernel 不引用具体 Provider Adapter。
8. `execution.py` 不包含协议具体错误字节。

## 4. 性能与兼容目标

在本机 fake upstream 环境下：

- 新增 OpenAI Gateway 的 routing overhead p95 小于 5 ms。
- OpenAI 首 event 的代理额外开销 p95 小于 20 ms。
- 至少 50 个并发 OpenAI SSE 连接无 event 串流。
- 不含上游耗时的 OpenAI/Anthropic 两条链路性能差异小于 10%。
- Provider 或 telemetry 暂时不可用不泄漏原始请求。

## 5. 里程碑

### M0：协议耦合拆分

- 提取 protocol-neutral RouterError。
- 提取 StreamSemantics 和 ErrorRenderer。
- 提取按协议的 Feature Extractor。
- 将 ProviderRegistry 返回类型改为 ProviderPort。

### M1：OpenAI Responses Gateway

- 实现 `/v1/responses`。
- 完成本地认证、request envelope、feature extraction、JSON/SSE response。

### M2：OpenAI Provider Adapter

- 实现 OpenAI upstream 请求、认证、并发、超时和 raw response。
- 完成 OpenAI error、usage 和 request ID 归一化。

### M3：双协议路由回归

- 扩展 ModelTarget、Capability、profile config 和 protocol hard filter。
- 完成 same-protocol fallback、state scope 约束和完整 fixture。

### M4：发布验收

- 完成 OpenAI-compatible client 人工验收。
- 完成 v0.1 Anthropic 回归。
- 完成性能、隐私和取消测试。

## 6. 架构不变量

- v0.2 只增加 OpenAI Adapter，不增加跨协议转换。
- Routing Kernel 不导入 OpenAI 或 Anthropic Gateway/Provider 模块。
- Execution Plan 中的 target protocol 必须与 inbound protocol 相同。
- Provider Adapter 不修改路由策略，只执行 resolved target。
- Stream Semantics 和 Error Renderer 是协议专属，Execution Engine 是协议无关的。
- raw passthrough 是同协议默认模式。
- `previous_response_id`、conversation 和 provider-managed tools 不得被 Router 自行解释成跨供应商语义。
- 原始请求、响应、reasoning 和 response state identifier 默认不进入 SQLite。
- v0.2 不改变 v0.1 已验收的 Anthropic HTTP interface。

## 7. 外部参考与验证说明

实现阶段必须以当前官方 OpenAI 文档和真实 Responses fixture 校准字段与 event 集合：

- [Responses API reference](https://platform.openai.com/docs/api-reference/responses)
- [Responses streaming guide](https://platform.openai.com/docs/guides/streaming-responses)

本环境在写 spec 时无法访问官方页面，因此文档中的字段和 event 列表是 v0.2 的实现基线，不替代最终 fixture 验证。任何与官方文档不一致的行为以官方 schema、真实响应和兼容性测试为准。
