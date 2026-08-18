# Coding LLM Router v0.3 Provider Reliability 执行计划

> 状态：Implemented and verified  
> 版本：v0.3  
> 日期：2026-08-18  
> 设计规格：[v0.3 Provider Reliability 设计规格](./coding-llm-router-v0.3-provider-reliability-spec.md)

## 1. 目标

在不引入跨协议转换的前提下，为现有 Anthropic Messages 和 OpenAI Responses 链路增加同协议 Provider/Model Target 健康管理。实现必须减少对已知不可用上游的重复调用，同时保持 RoutingKernel 的确定性、Execution Plan 的不可变性、SSE Commit Point、state scope、能力过滤和隐私默认值。

本计划只实现 v0.3 spec。Outcome 上报、离线回放、shadow policy、跨进程健康状态和主动探测不进入本轮。

## 2. 当前基线

当前实现已经具备：

- `POST /v1/messages` 和 `POST /v1/responses` 双协议入口。
- 协议硬过滤、能力过滤、显式/自动 profile 和 state scope。
- 有序 Execution Plan、pre-commit fallback、JSON/SSE 转发和协议专属错误渲染。
- Provider Registry、ProviderPort、SQLite RouteEvent 和 Prometheus metrics。

实施前必须正视以下现状：

1. 仓库当前没有 `tests/` 目录，v0.1/v0.2 的可执行回归资产未保存在当前工作树。
2. `ExecutionEngine.execute()` 用 target 枚举序号作为 attempt count；加入 health skip 后该语义会错误。
3. 路由阶段抛出的错误不会进入正常 completion telemetry。
4. Provider Adapter 把 transport failure 直接转换为 `RouterError`，缺少健康模块需要的 bounded failure class。
5. `record_completion()` 会把没有显式 Outcome Signal 的非成功交换当作 task failure，可能污染 session escalation。
6. `execution/engine.py` 已约 415 行，直接叠加健康逻辑会超过 500 行约束。

因此实现顺序不能从 HealthCoordinator 直接开始；必须先建立基线和整理现有 seam。

## 3. 依赖顺序

```mermaid
flowchart LR
    P0["P0 Baseline fixtures"] --> P1["P1 Streaming extraction"]
    P1 --> P2["P2 Domain and config"]
    P2 --> P3["P3 HealthCoordinator"]
    P3 --> P4["P4 Failure classification"]
    P3 --> P5["P5 Routing integration"]
    P4 --> P6["P6 Execution integration"]
    P5 --> P6
    P6 --> P7["P7 Gateway and session"]
    P7 --> P8["P8 Telemetry and metrics"]
    P8 --> P9["P9 Release acceptance"]
```

每个阶段必须独立通过其验证项后再进入下一阶段。不要把 P2-P8 合并成一个大提交。

## 4. 文件变更地图

### 4.1 新增源码

```text
src/llm_router/health/__init__.py
src/llm_router/health/models.py
src/llm_router/health/port.py
src/llm_router/health/coordinator.py
src/llm_router/execution/failures.py
src/llm_router/execution/streaming.py
```

### 4.2 修改源码

```text
src/llm_router/domain.py
src/llm_router/config.py
src/llm_router/routing/kernel.py
src/llm_router/execution/engine.py
src/llm_router/providers/port.py
src/llm_router/providers/anthropic.py
src/llm_router/providers/openai.py
src/llm_router/gateway/common.py
src/llm_router/gateway/anthropic.py
src/llm_router/gateway/openai.py
src/llm_router/gateway/errors.py
src/llm_router/gateway/renderers.py
src/llm_router/telemetry/sqlite_store.py
src/llm_router/telemetry/metrics.py
src/llm_router/app.py
```

### 4.3 配置、发布和文档

```text
router.example.yaml
setup.py
README.md
docs/coding-llm-router-v0.3-provider-reliability-spec.md
```

### 4.4 验证文件

仓库没有现成测试目录，v0.3 在明确验收范围内新增函数式 pytest，不创建测试 class：

```text
tests/conftest.py
tests/test_v02_regression.py
tests/test_health_config.py
tests/test_health_coordinator.py
tests/test_health_routing.py
tests/test_health_execution.py
tests/test_health_gateway.py
tests/test_health_telemetry.py
```

## 5. P0：冻结 v0.2 行为基线

### 5.1 工作项

- 在 `tests/conftest.py` 提供固定配置、Model Target、fake clock、FakeProviderPort 和 FakeStreamSemantics。
- FakeProviderPort 必须记录真实 invoke 次数，区分“计划遍历”与“上游调用”。
- 使用 `httpx.MockTransport` 或异步 fake exchange，不访问真实网络。
- 在 `tests/test_v02_regression.py` 固定以下行为：
  - Anthropic/OpenAI 协议硬过滤。
  - capability-equivalent fallback。
  - response state 不跨 state scope。
  - JSON 成功和 retryable pre-commit fallback。
  - SSE 首 event 后禁止 fallback。
  - 未知 JSON 字段和未知 SSE event 透传。
- 记录当前测试命令和基线结果；发现现有失败时先解释，不在 v0.3 中顺手改变无关行为。

### 5.2 完成条件

- 测试完全离线、可重复运行。
- 至少覆盖两套协议各一条 JSON 和 SSE 链路。
- 当前 v0.2 代码在不加健康功能时通过基线。

### 5.3 验证

```bash
.venv/bin/python -m pytest -q tests/test_v02_regression.py
.venv/bin/python -m compileall -q src tests
```

## 6. P1：提取流式实现，控制文件复杂度

### 6.1 工作项

- 把 `_SSEUsageTracker`、首 event 读取和 downstream stream relay 从 `execution/engine.py` 移到 `execution/streaming.py`。
- 保持 StreamSemantics 注入、首 event 校验、idle timeout、stream deadline、completion future 和 post-commit error 字节完全不变。
- `ExecutionEngine` 只保留 attempt 编排，不重新实现协议事件解析。
- 所有新增函数添加简短的 function-level comment/docstring。

### 6.2 完成条件

- 这是纯重构，不改变响应状态、header、event 顺序或错误格式。
- `execution/engine.py` 在加入健康逻辑后仍预留足够空间，最终不超过 500 行。
- P0 全量回归通过。

## 7. P2：领域对象与配置

### 7.1 `health/models.py`

新增并冻结：

- `HealthState`: `healthy/cooldown/half_open/blocked`。
- `FailureClass`: `success/provider_transient/target_transient/provider_permanent/target_permanent/request_rejected/client_cancelled/post_commit_stream_failure`。
- `TargetAvailability`。
- `AvailabilitySnapshot`。
- `HealthLease`。
- `AttemptOutcome`。
- `HealthTransition`，仅包含 bounded 字段。

全部跨模块传递对象使用 frozen dataclass 或不可变 enum。不得保存异常对象、响应正文或 API key。

### 7.2 `health/port.py`

定义稳定 interface：

```python
class HealthPort(Protocol):
    def snapshot(self, now: datetime) -> AvailabilitySnapshot:
        """Return one immutable availability view."""

    def acquire(self, target: ModelTarget, now: datetime) -> HealthLease | None:
        """Atomically admit a healthy call or one recovery probe."""

    def record(self, lease: HealthLease, outcome: AttemptOutcome) -> None:
        """Apply one sanitized attempt outcome."""
```

生产实现为 `InMemoryHealthCoordinator`；禁用健康功能时使用 `DisabledHealthCoordinator`，它始终返回 all-healthy snapshot 和普通 lease，并忽略 outcome。调用方不得散布 `if health.enabled` 分支。

### 7.3 `config.py`

- 增加顶层 `HealthConfig` 和 `RouterConfig.health`。
- 使用 spec 中的默认值和上下界。
- 验证 `max_cooldown_seconds >= cooldown_seconds`。
- 将 health 配置纳入 effective policy hash。
- 旧配置缺少 `health` 时使用默认值，不要求用户迁移文件。

### 7.4 验证

- 有效边界值可加载。
- 越界、倒置 cooldown 和未知字段被拒绝，错误日志保持英文。
- `enabled: false` 构造 Disabled Adapter。

## 8. P3：实现 HealthCoordinator 状态机

### 8.1 内部状态

- 分别维护 Provider domain 和 Target domain record。
- record 只包含 state、consecutive failures、window start、cooldown until、backoff level 和 half-open lease token。
- 使用短临界区同步锁保证 `snapshot/acquire/record` 原子性；锁内不得执行 I/O、日志格式化或 telemetry 写入。
- 状态容量严格由配置中的 Provider 和 Model Target 数量限定，不接受运行时任意 key。

### 8.2 状态转换

- 按 spec 实现 threshold、failure window、指数 backoff 和 max cooldown。
- Retry-After 只能延长 cooldown，且受 max cooldown 限制。
- cooldown 到期后第一个 acquire 原子转为 half-open 并发出 probe lease。
- half-open busy 时立即返回 `None`，不得等待 probe。
- success 重置相关 domain；request rejected 和 cancellation 保持中立。
- permanent failure 进入 blocked，只有新 coordinator/config generation 才恢复。

### 8.3 并发验证

- 固定 fake clock，不使用 sleep 驱动状态机测试。
- 100 个并发 acquire 竞争同一 half-open domain 时恰好一个成功。
- 过期或重复 lease outcome 不得重复更新状态。
- Provider cooldown 影响其全部 target；Target cooldown 不影响同 Provider 其他 target。

## 9. P4：建立 bounded failure classification

### 9.1 `providers/port.py`

- 新增 `ProviderFailure`，只承载安全 code、FailureClass 和可选 Retry-After。
- Anthropic/OpenAI Provider 的 transport exception 改为抛 `ProviderFailure(provider_transient)`。
- ProviderPort 仍然只负责建立原始交换，不读取或持久化响应正文。

### 9.2 `execution/failures.py`

实现纯函数：

- HTTP status 到 FailureClass 的默认映射。
- transport/timeout 到 FailureClass 的映射。
- Retry-After delta-seconds 和 HTTP-date 解析、负值处理和最大值裁剪。
- FailureClass 到客户端 RouterError/fallback_allowed 的映射。

默认规则严格按照 spec；不得根据普通错误 message 做字符串猜测。Provider 将来需要特殊语义时，只能通过 bounded metadata 扩展。

### 9.3 完成条件

- Execution Engine 不再直接用 `_RETRYABLE_STATUS` 同时承担 fallback 和 health 分类。
- Provider Adapter 不再向核心抛 Gateway 层的 `RouterError`。
- 401/404/429/5xx、transport timeout、request rejection 和 cancellation 都有确定断言。

## 10. P5：RoutingKernel 接入 Availability Snapshot

### 10.1 interface 调整

- `RoutingKernel.plan()` 增加必填 `AvailabilitySnapshot` 参数，不提供隐式 all-healthy 默认值。
- `ExecutionPlan` 增加 snapshot revision、health filtered count 和 bounded health reason。
- Gateway 是生产调用方；测试显式构造 snapshot。

### 10.2 决策实现

- 先计算“协议和能力匹配候选”，再做 health filter，保证 422 与 503 可区分。
- 保留 explicit profile 中健康 target 的配置顺序。
- half-open target 可以留在 plan 内，由 Execution Engine 竞争 lease。
- auto profile 保留 desired tier 语义；只允许使用 capability-equivalent、same-protocol target。
- state scope 基于健康过滤后的 primary 重新约束 fallback。
- 没有能力返回 `router_no_capable_model`；有能力但全不可用返回 `router_no_available_target`。

### 10.3 错误

- 在 `gateway/errors.py` 增加 503 `no_available_target(retry_after)`。
- Anthropic/OpenAI ErrorRenderer 添加整数秒 `Retry-After` header。
- Renderer 不暴露 Provider、Model Target、原始错误或上游 URL。

## 11. P6：Execution Engine 接入 Health Lease

### 11.1 计数语义修复

循环中分离：

- `target_index`：Execution Plan 中的位置。
- `event_sequence`：attempt/health skip telemetry 顺序。
- `upstream_attempt_count`：真正调用 ProviderPort 的次数。

Health skip 生成 `AttemptEvent(status="health_skipped")`，但不增加 `upstream_attempt_count`。`ProxyResponse.attempt_count` 和路由响应 header 只使用实际上游调用数。

### 11.2 attempt 生命周期

- 每次 invoke 前 acquire Health Lease。
- lease 失败立即检查下一 target，不获取 Provider semaphore。
- pre-commit failure 在决定 fallback 前 record outcome。
- JSON 只有完整读取 2xx body 后记录 success。
- SSE 把 lease 交给 stream relay；终止事件后记录 success，idle/transport/upstream error 后记录 post-commit failure。
- cancellation 记录 neutral outcome 并关闭 exchange。
- 使用单次完成守卫，确保每个 lease 最多 record 一次。

### 11.3 stale plan

如果 plan 生成后所有 target 都因竞态拿不到 lease：

- 不发送上游请求。
- 从最新 snapshot 计算 Retry-After。
- 返回 `router_no_available_target`。
- attempt count 为 0，health skipped 数量保留在失败 telemetry 中。

### 11.4 文件约束

- `engine.py` 只编排 plan、deadline、lease 和 fallback。
- SSE byte/event 实现留在 `streaming.py`。
- failure mapping 留在 `failures.py`。
- 任一文件超过 500 行前继续按语义拆分，不创建 pass-through wrapper。

## 12. P7：Gateway、Runtime 与 Session 解耦

### 12.1 `app.py`

- Runtime 增加 `health: HealthPort`。
- `create_app()` 根据配置构造 InMemory 或 Disabled Adapter，并注入 ExecutionEngine。
- Health transition callback 在锁外连接 structured logging 和 RouterMetrics。
- FastAPI 与 package version 更新为 `0.3.0`。

### 12.2 两套 Gateway

- Feature Extractor 完成后获取同一时刻的 snapshot，再调用 `kernel.plan(request, snapshot)`。
- 不在 Gateway 里实现 health filter、状态转换或 failure mapping。
- 两个 Gateway 对 503 使用各自现有 ErrorRenderer。

### 12.3 Session 规则

修改 `record_completion()`：

- 只有请求中已经提取出的明确 Outcome Signal 才更新 session。
- health skipped、无可用 target、transport、429、5xx、认证失败和客户端取消都不更新 session。
- 普通 2xx 只能证明 Provider 交换健康，不能自动推断任务成功。

### 12.4 路由阶段失败 telemetry

新增共享 `record_route_failure()`，供两个 Gateway 在路由阶段 503 时调用：

- `attempt_count=0`。
- `primary_model/final_model` 使用稳定 bounded sentinel `none`，兼容当前 SQLite NOT NULL schema。
- 记录 health snapshot revision、filtered/skipped count 和安全 route reason。
- 不写 session state，不保存请求正文。

## 13. P8：Telemetry、SQLite 和 Metrics

### 13.1 Domain 与 SQLite

- RouteEvent 增加 spec 定义的 5 个 health 字段，提供向后兼容默认值。
- AttemptEvent 明确 `sequence` 是 telemetry event order，status 可为 `health_skipped`。
- SQLite `route_requests` 使用 `_ensure_columns()` 做 additive migration。
- `route_attempts` 不改主键；health skip 与实际 attempt 使用唯一 event sequence。
- 不持久化实时 Health State，不新增 health snapshot 表。

### 13.2 Metrics

增加：

- health state gauge。
- cooldown transition 和累计 cooldown counter/histogram。
- probe/success/failure counter。
- health skipped counter。
- no available target counter。
- health update failure counter。

所有 label 只使用配置中有界的 protocol/provider/target/state/failure class。日志 message 必须为英文，例如 `provider health state changed`，细节放在 structured extra 字段。

### 13.3 非阻断要求

- Metrics 或 telemetry 更新失败不能改变客户端响应。
- Health state 本身更新必须完成；只有 transition observer/telemetry 可以 best effort。
- 不在 health lock 内写 SQLite 或 Prometheus。

## 14. P9：配置、发布与最终验收

### 14.1 发布文件

- `router.example.yaml` 增加 health block，`routing.policy_version` 更新为 `v2`。
- `setup.py` 和 FastAPI version 更新为 `0.3.0`。
- README 增加 same-protocol health routing、配置示例和“不做协议转换”说明。
- 验收完成后将 spec 状态从 Draft 改为 Accepted，并记录验收日期。

### 14.2 自动验证

```bash
.venv/bin/python -m compileall -q src tests
.venv/bin/python -m pytest -q
.venv/bin/ruff check src tests
.venv/bin/mypy src
```

### 14.3 故障演练

使用 fake upstream，不访问真实供应商：

1. 连续两次 503 后 primary cooldown，第三次请求直接走 fallback。
2. cooldown 到期后 100 个并发请求只有一个 probe primary，其余走 fallback。
3. probe 成功恢复 primary；probe 失败 backoff 增长且受 max 限制。
4. Provider 401 blocked；同 Provider target 全部跳过。
5. 单模型 404 只 blocked 该 target。
6. 429 读取 Retry-After，但不超过 max cooldown。
7. JSON success、SSE success、SSE post-commit failure 和 client cancellation 分别产生正确 outcome。
8. 所有 target unavailable 返回双协议各自的 503 JSON 和 `Retry-After`。
9. health disabled 时 v0.2 调用次数、fallback 顺序和响应完全一致。
10. SQLite、metrics 和日志不包含 body、tool 参数、reasoning、key 或完整错误 body。

### 14.4 性能验收

- 1000 target 的 snapshot p95 小于 1 ms。
- 100 并发 acquire/record p95 额外开销小于 2 ms。
- 不产生 synthetic upstream request。
- 并发 SSE 不串流，post-commit failure 不触发 fallback。

## 15. 完成定义

只有同时满足以下条件，v0.3 才能标记完成：

- P0-P9 全部通过且不存在跳过的核心验收项。
- Anthropic/OpenAI 两条协议继续 same-protocol raw passthrough。
- RoutingKernel 不读取 HealthCoordinator、时钟、网络、锁或数据库。
- Execution Engine 不包含协议专属 SSE 字节。
- health skip 与 upstream attempt count 语义分离。
- session escalation 不再吸收 Provider 基础设施故障。
- SQLite migration 可在现有 v0.2 数据库上重复执行。
- 所有日志 message 为英文，所有持久化和 metrics 字段均有界且脱敏。
- 修改后的源码文件均不超过 500 行，并通过 compile、pytest、Ruff 和 mypy。
