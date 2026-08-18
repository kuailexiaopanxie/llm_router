# Coding LLM Router v0.3 Provider Reliability 设计规格

> 状态：Accepted  
> 版本：v0.3  
> 日期：2026-08-18  
> 前置版本：[v0.2 OpenAI Responses 设计规格](./coding-llm-router-v0.2-openai-responses-spec.md)  
> 实施计划：[v0.3 Provider Reliability 执行计划](./coding-llm-router-v0.3-provider-reliability-implementation-plan.md)

## 1. 摘要

v0.3 为现有 Anthropic Messages 和 OpenAI Responses 路由增加基于运行时健康状态的可用性判断。它在请求进入 RoutingKernel 前提供一个不可变的 Availability Snapshot，在 Execution Engine 真正调用上游前再次获取短生命周期的 Health Lease，从而避免把请求持续发送到已经失败或处于 cooldown 的 Provider/Model Target。

本版本不做 Anthropic/OpenAI 协议转换。`target.protocol` 必须始终等于入站 `request.protocol`。用户通过配置选择符合目标协议的 Provider；Router 不承担供应商之间的语义映射。

```text
Protocol Gateway
  -> Feature Extractor
  -> Health Coordinator.snapshot()
  -> RoutingKernel.plan(request, availability)
  -> Execution Engine
  -> Health Coordinator.acquire(target)
  -> Provider Adapter
  -> Health Coordinator.record(attempt outcome)
  -> Telemetry Recorder
```

## 2. 问题与目标

v0.2 的 fallback 只会在一次上游调用已经失败后才尝试下一个 target。对于持续 429、5xx、连接失败或上游配置失效的 Provider，这会导致：

- 每个请求都重复支付一次失败调用的延迟。
- 并发请求同时撞向已知不可用的 Provider。
- 路由原因无法区分“没有能力”与“暂时不可用”。
- session failure escalation 可能把供应商故障误认为任务失败。

v0.3 目标：

1. 对 Provider 和 Model Target 维护进程内、有界、可过期的健康状态。
2. 在路由计划生成时过滤 cooldown 或 blocked 的候选。
3. 在并发场景下只允许一个 Recovery Probe 进入 half-open failure domain。
4. 使用 retryable failure、Retry-After 和指数退避计算 cooldown。
5. 维持 v0.2 的 same-protocol fallback、能力过滤、state scope 约束和 Commit Point 语义。
6. 将健康失败与任务失败、session failure escalation 分开记录。
7. 在不保存原始请求、响应或密钥的前提下提供健康状态指标和路由原因。

## 3. 非目标

- Anthropic Messages 与 OpenAI Responses 的协议转换。
- Chat Completions、Realtime、Batch 或其他新入站协议。
- LiteLLM、统一内部消息模型或供应商语义兼容层。
- 跨进程、跨 worker、Redis 或数据库共享健康状态。
- 主动发送 synthetic probe、模型质量 judge 或生成结果评分。
- 根据成功率、成本或答案质量重新排序健康 target。
- 自动修改配置、自动轮换密钥或自动修改客户端配置。
- 新增用户级请求限流；Provider 已有的 `max_concurrency` 继续生效。
- Dashboard、管理后台和多租户控制平面。

## 4. 核心原则与架构不变量

### 4.1 健康是可用性过滤器

Health State 只表达是否值得尝试连接并完成一次上游交换，不表达生成质量。健康状态不能把便宜模型自动升级成高质量模型，也不能改变 `fast/balanced/deep` 的策略顺序。

### 4.2 RoutingKernel 保持纯决策

RoutingKernel 不读取时钟、网络、锁、Provider Registry 或数据库。相同的请求、配置、session state 和 Availability Snapshot 必须生成相同的 Execution Plan。

```python
class RoutingKernel:
    def plan(
        self,
        request: RoutingRequest,
        availability: AvailabilitySnapshot,
    ) -> ExecutionPlan:
        """Build an immutable plan from request facts and an availability snapshot."""
```

### 4.3 Execution Engine 负责竞态复核

Availability Snapshot 可能在计划生成后过期。Execution Engine 在每次实际上游调用前必须通过 Health Coordinator 获取 Health Lease；获取失败时跳过 target，不发送请求，并继续检查计划内的下一个 target。

### 4.4 协议不转换

```text
target.protocol == request.protocol
```

该条件适用于 primary、fallback 和 auto routing 的所有候选。健康状态不能放宽协议约束。

### 4.5 Commit Point 不可逆

健康状态可以影响尚未开始的 attempt，但不能中断已经向客户端提交首字节的流，也不能在流中途切换 target。流结束后的健康记录由该 attempt 的最终 Attempt Outcome 更新。

### 4.6 健康状态不持久化

健康状态只存在于单进程内存。进程重启后所有 Provider 和 Model Target 从 `healthy` 开始，不执行启动探测。SQLite 只记录脱敏的健康相关观测，不作为实时健康状态源。

## 5. 领域模型

### 5.1 Failure Domain

v0.3 使用两层 failure domain：

| 类型 | 标识 | 影响范围 | 典型失败 |
|---|---|---|---|
| Provider domain | `provider` 名称 | 该 Provider 下所有 Model Target | DNS、TLS、连接超时、响应头超时、5xx、Provider 认证失败 |
| Target domain | Model Target alias | 单个 Model Target | 目标模型 404、模型级 429、模型不可用 |

Provider 是用户配置的独立 base URL、凭据和并发范围。若两个账号或 endpoint 互相独立，必须配置成两个 Provider，健康状态也随之隔离。

### 5.2 Health State

每个 failure domain 只有以下状态：

| 状态 | 含义 | 是否允许普通请求 |
|---|---|---|
| `healthy` | 最近没有达到 cooldown 条件 | 允许 |
| `cooldown` | 暂时抑制请求，等待 `cooldown_until` | 不允许 |
| `half_open` | cooldown 到期，只等待一个 Recovery Probe | 仅 probe 允许 |
| `blocked` | 配置或权限类失败，直到配置代际改变或进程重启 | 不允许 |

`half_open` 不是永久状态。Recovery Probe 成功转为 `healthy`，可恢复失败重新进入 `cooldown`。

### 5.3 Availability Snapshot

```python
@dataclass(frozen=True)
class AvailabilitySnapshot:
    """Immutable health view used by one routing decision."""

    revision: int
    observed_at: datetime
    target_states: Mapping[str, TargetAvailability]
    earliest_recovery_at: datetime | None
```

`TargetAvailability` 至少包含：

- `eligible: bool`
- `state: healthy | cooldown | half_open | blocked`
- `reason: healthy | provider_cooldown | target_cooldown | provider_blocked | target_blocked | probe_eligible`
- `retry_at`，仅在可安全暴露恢复时间时使用

Snapshot 不包含错误正文、请求内容、响应内容、API key 或完整上游 URL。

### 5.4 Health Lease

```python
class HealthCoordinator(Protocol):
    def snapshot(self, now: datetime) -> AvailabilitySnapshot:
        """Return an immutable view without network or persistence side effects."""

    def acquire(self, target: ModelTarget, now: datetime) -> HealthLease | None:
        """Atomically admit one healthy request or one half-open recovery probe."""

    def record(self, lease: HealthLease, outcome: AttemptOutcome) -> None:
        """Update bounded in-memory health state from one sanitized outcome."""
```

Health Lease 必须绑定 Provider、Model Target、snapshot revision 和 acquisition time。没有 Lease 的 target 不得被 Provider Adapter 调用。

## 6. Failure 分类

Health Coordinator 只接受有限的 Attempt Outcome，不接受任意异常文本：

| Outcome class | 健康影响 | 是否允许 fallback |
|---|---|---|
| `success` | 重置相关 failure domain 的连续失败计数并恢复 `healthy` | 否 |
| `provider_transient` | 记录 Provider failure，达到阈值后 cooldown | 是 |
| `target_transient` | 记录 Model Target failure，达到阈值后 cooldown | 是 |
| `provider_permanent` | Provider 进入 `blocked` | 否 |
| `target_permanent` | Model Target 进入 `blocked` | 否 |
| `request_rejected` | 不改变健康状态 | 否 |
| `client_cancelled` | 不改变健康状态 | 否 |
| `post_commit_stream_failure` | 记录对应 failure domain；不触发当前请求 fallback | 否 |

默认映射：

- DNS、TLS、connect timeout、response-header timeout：`provider_transient`。
- 500、502、503、504、529：`provider_transient`，除非 Adapter 提供明确的 target scope。
- 429：默认 `target_transient`；配置了 Provider/account 级限流语义时可标记为 `provider_transient`。
- 401 或明确的 Provider credential failure：`provider_permanent`。
- 404 model not found 或明确的 target access failure：`target_permanent`。
- 400、422、tool schema rejection 和客户端请求错误：`request_rejected`。
- 客户端断开或主动取消：`client_cancelled`。

Provider Adapter 可以提供有限的 `failure_scope` metadata，但不得把原始错误正文交给健康模块。

## 7. 状态转换与 cooldown

### 7.1 转换

```text
healthy --retryable failure threshold reached--> cooldown
healthy --permanent failure-------------------> blocked
cooldown --cooldown_until reached--------------> half_open
half_open --one probe success-----------------> healthy
half_open --probe retryable failure------------> cooldown
healthy/half_open --request rejected/cancelled-> unchanged
```

成功定义为：非流式响应完整读取且状态为 2xx；流式响应至少完成协议允许的终止事件且没有 idle、transport 或上游 error。仅收到第一个 SSE event 不足以重置连续失败计数。

### 7.2 默认参数

```yaml
health:
  enabled: true
  failure_threshold: 2
  failure_window_seconds: 120
  cooldown_seconds: 30
  max_cooldown_seconds: 300
  backoff_multiplier: 2.0
```

规则：

1. 连续 retryable failure 达到 `failure_threshold` 才进入 cooldown。
2. 超过 `failure_window_seconds` 没有同域失败时，连续失败计数衰减为零。
3. 第一次 cooldown 使用 `cooldown_seconds`；连续恢复失败按 `backoff_multiplier` 指数增加，不能超过 `max_cooldown_seconds`。
4. 有效的上游 Retry-After 可以延长 cooldown，但不能超过 `max_cooldown_seconds`。
5. `blocked` 不自动过期；配置重新加载或进程重启后才重新评估。
6. 同一 half-open failure domain 同时只能持有一个 Health Lease。其他请求必须跳过该 target，不能排队等待 probe。

v0.3 不主动发健康探测。Recovery Probe 使用真实客户端请求，并且遵循原有能力、协议、state scope 和 Commit Point 约束。

## 8. 路由行为

RoutingKernel 的决策顺序调整为：

```text
resolve requested profile
  -> derive required capabilities
  -> filter protocol mismatch
  -> filter incapable targets
  -> filter blocked/cooldown targets from Availability Snapshot
  -> apply explicit profile or auto tier ordering
  -> apply session escalation
  -> apply state_scope constraints
  -> retain at most one probe-eligible target per failure domain
  -> emit immutable Execution Plan
```

### 8.1 Explicit profile

保留配置中的 primary/fallback 顺序。健康过滤只删除当前不可用的 target，不重排健康 target 的成本或质量顺序。

若 primary 处于 cooldown 而 fallback healthy，fallback 直接成为本次 plan 的 primary，并增加 `health_cooldown_filtered` route reason。若 primary 进入 half-open，允许它保留在原顺序中，由 Execution Engine 竞争 Health Lease；未取得 Lease 的请求继续使用 fallback。

### 8.2 Auto profile

`code/auto` 先按照 v0.2 规则确定 desired tier，再在该协议和能力集合内使用健康 target。健康状态不能把请求从 deep 降级到不具备所需能力的 fast target。

如果目标 tier 没有健康 target，但同协议的其他 tier 存在能力等价 target，可以使用该 target，并增加 `health_tier_fallback`。如果只有不具备必需能力的 target，返回 `422 router_no_capable_model`，不能为了可用性删除能力要求。

### 8.3 全部不可用

当存在能力匹配的 target，但全部处于 cooldown、half-open busy 或 blocked：

- 返回协议兼容的 `503 router_no_available_target`。
- 如果存在最近恢复时间，响应包含整数秒 `Retry-After`。
- 不执行无意义的上游调用。
- 不增加 session 的 task failure escalation。
- route reason 使用 `health_no_available_target`，不得暴露完整供应商错误。

“没有能力”仍然使用 `422 router_no_capable_model`；“有能力但暂不可用”不得复用该错误。

## 9. Execution Engine 集成

### 9.1 Attempt 前

对 `ExecutionPlan.targets` 逐项处理：

1. 调用 `HealthCoordinator.acquire()`。
2. 获取不到 Lease 时追加 `AttemptEvent(status="health_skipped")`，不调用 Provider，不消耗 upstream attempt limit。
3. 获取到 Lease 后按 v0.2 逻辑执行认证、超时、响应头校验、JSON/SSE 读取和 fallback。

健康跳过是计划内的本地决策，不计入 `attempt_count`；`attempt_count` 仍只表示实际发出的上游调用次数。

### 9.2 Attempt 后

- pre-commit retryable/permanent failure：立即向 Health Coordinator 记录 outcome，再按 v0.2 fallback 规则决定是否继续。
- 非流式完整成功：记录 `success`。
- 流式响应：把 Health Lease 和最终 completion callback 绑定；正常终止后记录 `success`，idle/transport/upstream error 后记录 `post_commit_stream_failure`。
- 客户端取消：关闭 exchange，记录 `client_cancelled`，不惩罚 Provider。

Health Coordinator 的更新失败不能破坏主请求；执行结果已经确定后，健康观测可以丢弃并增加 telemetry/health update failure metric。

### 9.3 Session State 解耦

下列情况不得增加 session consecutive failures：

- Health skipped。
- `router_no_available_target`。
- Provider transport、429、5xx 或认证故障导致的全链路失败。

只有已提交的任务语义信号和 v0.2 已定义的 tool result outcome 才能更新 session failure escalation。Provider 健康成功也不代表任务成功。

## 10. 配置变化

在现有 `router.yaml` 顶层增加可选 `health` block。缺省配置按第 7.2 节启用健康管理；`enabled: false` 时恢复 v0.2 的候选过滤和 fallback 行为，但不恢复跨协议能力。

```yaml
health:
  enabled: true
  failure_threshold: 2
  failure_window_seconds: 120
  cooldown_seconds: 30
  max_cooldown_seconds: 300
  backoff_multiplier: 2.0
```

配置校验：

- `failure_threshold`：1-10。
- `failure_window_seconds`：10-86400。
- `cooldown_seconds`：1-3600。
- `max_cooldown_seconds`：不小于 `cooldown_seconds`，最大 86400。
- `backoff_multiplier`：1.0-8.0。
- 所有 target chain 继续执行 v0.2 的 protocol、capability-equivalence 和 state_scope 校验。
- 不允许通过配置声明跨协议 fallback。

示例中的 `routing.policy_version` 更新为 `v2`。旧配置没有 `health` 时可直接加载，也不需要修改客户端请求。新增 telemetry 列由 SQLite store 在启动时自动执行 additive migration，不要求用户运行手工迁移命令。

## 11. 观测与指标

### 11.1 RouteEvent

在现有脱敏 `RouteEvent` 中增加 bounded 字段：

```text
health_enabled: bool
health_snapshot_revision: int
health_filtered_count: int
health_skipped_count: int
health_reason: str | None
```

`AttemptEvent.status` 增加 `health_skipped`。健康状态转换另记录结构化事件：

```text
health_state_changed
provider
target_alias | null
from_state
to_state
failure_class | null
cooldown_seconds | null
```

不得记录：请求正文、响应正文、tool 参数、reasoning、API key、完整错误 body、previous response ID。

### 11.2 Metrics

至少增加：

- 当前 `healthy/cooldown/half_open/blocked` target 数量。
- cooldown 进入次数和累计时长。
- Recovery Probe 次数、成功次数和失败次数。
- health skipped target 数量。
- `router_no_available_target` 次数。
- 健康更新失败次数。

Metrics label 只能来自配置中的有限枚举：protocol、provider、target alias、health state、failure class。不得使用 request ID、session ID 或错误 message。

`/health` 继续只表示进程存活；`/ready` 继续只表示本地配置、telemetry 和内部模块已初始化，不执行上游主动探测。

## 12. 错误与响应

新增内部错误：

```text
router_no_available_target
HTTP 503
fallback_allowed = false
safe_message = "No capable upstream is temporarily available."
```

Anthropic 和 OpenAI Renderer 分别将其转换成协议兼容 JSON。`Retry-After` 只返回剩余 cooldown 的整数秒，不返回 Provider 名称或上游 Retry-After 原文。

健康过滤不能覆盖上游真实成功响应的 body、model、response ID、tool call ID 或 SSE event。健康模块只观察交换结果，不改写协议内容。

## 13. 性能与并发要求

- `snapshot()` 在 1000 个 Model Target 以内为纯内存操作，p95 小于 1 ms。
- `acquire()` 和 `record()` 使用进程内非阻塞锁或等价机制；100 个并发请求下健康协调额外 p95 开销小于 2 ms。
- v0.3 不增加任何 synthetic upstream request。
- 健康状态容量必须有界：Provider 数量、Model Target 数量和 failure domain 数量均受配置规模限制。
- 单个 Provider 的并发上限继续由现有 `max_concurrency` 控制；Health Lease 不替代 semaphore。

## 14. 测试与验收

### 14.1 状态机

1. 单次 retryable failure 不触发 cooldown。
2. 达到 threshold 后，后续请求不再调用该 failure domain。
3. cooldown 未到期时始终跳过；到期后并发请求只有一个 half-open probe。
4. probe 成功回到 healthy；probe 失败按 backoff 重新 cooldown。
5. Retry-After 延长 cooldown 但不超过 max。
6. permanent provider failure 只阻断该 Provider；target permanent failure 不阻断同 Provider 的其他 target。
7. request rejected 和 client cancellation 不改变健康状态。
8. 进程重启后健康状态清空。

### 14.2 路由与执行

1. Anthropic request 永远不选择 OpenAI Responses target，反之亦然。
2. cooldown primary 会使用 capability-equivalent fallback。
3. 健康过滤不会绕过 tools、thinking/reasoning、vision、structured output 或 state scope 约束。
4. 全部 target 不可用返回 503，而不是 422。
5. health skipped 不计入 upstream attempt count，也不触发 session failure escalation。
6. pre-commit failure 可以 fallback；post-commit stream failure 不可以。
7. 禁用 health 后 v0.2 fixture 全部保持通过。

### 14.3 Provider 与流

1. JSON 2xx 完整读取才重置健康状态。
2. SSE 首 event 校验失败记录 pre-commit failure 并允许 fallback。
3. SSE 正常结束记录 success。
4. SSE idle、transport error 和上游 error event 记录 post-commit stream failure，但不切换客户端响应。
5. 客户端取消不惩罚 Provider。

### 14.4 隐私与观测

1. SQLite 和日志不包含原始 body、密钥、响应正文或完整错误 body。
2. 所有健康 metrics label 都是有界枚举。
3. telemetry 写入失败不会导致主请求失败。
4. `Retry-After` 不泄露供应商内部信息。

## 15. 实施里程碑

### M0：领域对象与配置

- 增加 `HealthState`、`FailureClass`、`AttemptOutcome`、`AvailabilitySnapshot` 和 `HealthLease`。
- 增加 `HealthConfig` 和配置校验。
- 保持 v0.2 默认 protocol hard filter。

### M1：Health Coordinator

- 实现 Provider/Target 双层 failure domain。
- 实现状态转换、指数 cooldown、Retry-After 上限和 half-open 单 probe lease。
- 增加 fake clock 和 in-memory coordinator 测试 seam。

### M2：RoutingKernel 集成

- 将 Availability Snapshot 作为显式输入。
- 增加 health filtering、route reason 和 `router_no_available_target`。
- 验证 auto、explicit、stateful 和 capability-equivalence 组合。

### M3：Execution Engine 集成

- 在每次 Provider invoke 前 acquire lease。
- 为 pre-commit、JSON completion、SSE completion、cancel 和 post-commit error 记录 outcome。
- 保持现有 fallback、Commit Point 和 protocol-specific stream semantics。

### M4：观测与回归

- 扩展 SQLite schema、RouteEvent、AttemptEvent 和 metrics。
- 完成双协议 fixture、并发 probe、重启清空和 privacy 测试。

### M5：发布验收

- 更新示例配置和 policy version。
- 运行 v0.1 Anthropic 与 v0.2 OpenAI Responses 全量回归。
- 在 fake upstream 和可控故障注入环境完成性能与故障演练。

## 16. 版本边界与后续演进

v0.3 仍然是单进程、本地优先的可靠性增强。v0.4 才讨论显式 Outcome 上报和请求回放；v0.5 再讨论 shadow policy 和策略对比。跨协议转换不属于默认路线，除非未来出现明确的供应商或客户端需求，并且必须作为独立 Adapter 重新立项。

v0.3 的成功标准不是“更快切换更多模型”，而是：在同一入站协议下，Router 能够识别暂时不可用的 failure domain，减少重复失败调用，同时不破坏能力约束、fallback、流式语义、session 语义和隐私默认值。
