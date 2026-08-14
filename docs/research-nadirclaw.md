# NadirClaw 项目研究记录

> 分析对象：`/Users/fankai/Documents/work/personal/NadirClaw`
>
> 代码基线：`main`，commit `e10cf977ac90e94569e7532f3b0ade615337e8d6`（2026-08-12），tag `v0.22.0`。

## 一句话结论

NadirClaw 是一个运行在本机的 LLM 反向代理/模型路由器：对 OpenAI 和 Anthropic 兼容请求做鉴权、输入治理、复杂度分类、路由修正、上下文压缩、模型调用、故障切换、成本记录和观测，然后把请求直接转发到用户配置的模型供应商。它的核心商业价值是把“所有请求都打到高价模型”改成“简单请求先走便宜模型，只有必要时才升级”。

## 主要作用

1. **成本优化**：通过 prompt 复杂度分类选择 simple/complex（可扩展 mid/reasoning）模型；支持 prompt cache、上下文优化、成本估算、预算告警。
2. **协议兼容**：提供 OpenAI 风格 `/v1/chat/completions`，以及面向 Claude Code/Anthropic SDK 的 `/v1/messages` 和 `/v1/messages/count_tokens`。
3. **多供应商抽象**：Gemini 走原生 Google GenAI SDK；其他模型默认走 LiteLLM，因此可覆盖 OpenAI、Anthropic、Ollama 以及 LiteLLM 支持的供应商。
4. **可靠性**：按模型限流、429/5xx/超时 fallback chain、可选 provider health cooldown、真实 SSE 流式转发。
5. **本地运维**：JSONL + SQLite 请求日志、Prometheus `/metrics`、可选 OpenTelemetry、终端/本地 Web dashboard、CLI 报表和导出。

README 对产品定位、路由/验证/升级理念和兼容客户端的说明见 `README.md:37-73`、`README.md:174-205`、`README.md:1248-1299`。

## 实际运行链路

默认 `nadirclaw serve` 的主链如下：

```text
客户端
  -> FastAPI /v1/chat/completions 或 /v1/messages
  -> 本地 auth + 请求限流 + 尺寸校验 + prompt guard
  -> 取最近 user prompt，调用 classifier
  -> routing modifiers：agentic / reasoning / agent role / vision / context window / session cache / model pool
  -> context optimize（可选）
  -> context compression（可选）
  -> prompt cache（非流式，可选）
  -> per-model rate limiter
  -> Gemini 原生 SDK 或 LiteLLM
  -> 失败时按 fallback chain 尝试其他模型
  -> 预算记录、JSONL/SQLite 日志、Prometheus 指标、OTel span
  -> OpenAI JSON 或 SSE 返回
```

关键实现：

- FastAPI app、中间件、请求模型、启动预热和分类接口在 `nadirclaw/server.py:180-245`、`nadirclaw/server.py:431-630`。
- `/v1/chat/completions` 的校验、profile/direct/smart routing 分支在 `nadirclaw/server.py:1342-1490`。
- 优化和压缩顺序在 `nadirclaw/server.py:1492-1570`；模型调用和缓存/fallback 入口在 `nadirclaw/server.py:1200-1330` 及后续 completion 逻辑。
- `/v1/messages` 保留原始 Anthropic JSON，只改写 `model`，并对非流式/流式响应做 bounded reconcile 和 SSE passthrough，见 `nadirclaw/server.py:2826-3005`。
- `/v1/models` 同时返回 Anthropic/OpenAI 两种字段；健康、缓存、预算、限流、Prometheus 端点见 `nadirclaw/server.py:3060-3168`。

## 架构分层

### 1. 接入层

- **Web/API**：FastAPI + Uvicorn + Pydantic + `sse-starlette`。
- **CLI**：Click 命令组，覆盖 setup、serve、classify、optimize、report、dashboard、savings、budget、cache、export、auth 及各客户端 onboard。
- **客户端适配**：OpenAI-compatible 与 Anthropic-compatible 两套入口；对 Claude Code 还提供 settings/shim/daemon 安装逻辑。

### 2. 安全和治理层

- 本地 token auth，常量时间比较；默认只允许本机 CORS，显式配置后才打开指定 origin。
- Security headers、请求大小/参数边界校验、验证错误脱敏。
- 可选 prompt injection guard；可选非流式 PII redaction。
- 日志默认只记摘要，`NADIRCLAW_LOG_RAW` 才写原始消息/响应；日志中对常见 API key 形状做脱敏。

这些基线在 `server.py` 的中间件和 `CHANGELOG.md` 的 v0.17.0 security 条目中有明确记录。

### 3. 分类与路由层

**默认分类器**是 `BinaryComplexityClassifier`：

- 用 embedding encoder 生成 prompt 向量；与随包 `.npy` simple/complex centroid 做 cosine similarity。
- 置信度低于 `NADIRCLAW_CONFIDENCE_THRESHOLD` 时偏向 complex，避免复杂请求被降级。
- 如果配置 mid tier，再用 score threshold 映射 simple/mid/complex；否则保持二元路由。
- `get_classifier()` 支持 `binary`、可选本地 DistilBERT 三分类、可选远程 Morph classifier；后两者失败时回退 binary。

源码：`nadirclaw/classifier.py:31-240`、`nadirclaw/classifier.py:326-420`；配置说明：`README.md:381-416`。

**训练模型分支**：仓库还包含 `wide_deep_*` checkpoint。它是 BGE-base-en-v1.5 768 维 embedding + 33 维结构特征 + MLP/BatchNorm/三分类 head，并带 cost-sensitive/complex-gate 解码，见 `nadirclaw/wide_deep_classifier.py`。这套模型随包提供，但默认 HTTP 路径的 `get_classifier()` 仍按环境变量选择 binary/distilbert/morph，没有在 `server.py` 中直接切换到 wide-deep。

**路由修正器** `apply_routing_modifiers()` 按顺序叠加：

- agentic：工具定义、tool 消息、assistant-tool 循环、长 system prompt、深会话；
- reasoning：中英文 reasoning marker；
- opt-in agent role：planning/explore/subagent；
- vision：图片输入时交换到有视觉能力的模型；
- context window：按内置模型 registry 粗估 token，超窗时换候选模型；
- session cache：同一会话只允许升档，不允许降档；
- model pool：对等价模型做加权随机选择。

源码：`nadirclaw/routing.py:279-414`、`nadirclaw/routing.py:720-930`、`nadirclaw/routing.py:1039-1200`。

### 4. 模型适配层

- **Gemini**：`google-genai` 原生 SDK；同步 SDK 被放入有界线程池，支持 API key、OAuth/Vertex、ADC 和流式。
- **其他模型**：LiteLLM `acompletion`，统一传递 tools、tool_choice、reasoning/thinking、response_format、多模态内容。
- **Anthropic OAuth 特例**：`sk-ant-oat*` 不走 LiteLLM 的 `x-api-key`，而是由 `httpx` 直接发 Bearer 请求到 Anthropic，并做 thinking/max_tokens/system 参数兼容修复。
- **Ollama**：工具调用时把 `ollama/` 升级成 `ollama_chat/`，避免使用不支持 tools 的生成接口。

源码：`nadirclaw/server.py:760-1200`；依赖声明：`pyproject.toml:24-34`。

### 5. 可靠性与成本层

- `_call_with_fallback()` 按 tier-specific/global chain 依次尝试，provider health 开启后会跳过冷却中的候选；见 `nadirclaw/server.py:1200-1310`、`nadirclaw/provider_health.py`。
- `ModelRateLimiter` 负责每模型 RPM 保护；请求级 `_RateLimiter` 负责用户滑动窗口；见 `nadirclaw/rate_limit.py` 和 `server.py:138-175`。
- Prompt cache 是线程安全的内存 LRU + TTL，只对非流式路径生效；见 `nadirclaw/cache.py`。
- BudgetTracker 依据内置 model registry 的 input/output 单价累计 daily/monthly spend，并可 webhook/stdout 告警；见 `nadirclaw/budget.py`、`nadirclaw/routing.py` 的 `MODEL_REGISTRY`/`estimate_cost()`。

### 6. 观测与持久化层

- `requests.jsonl`：低延迟追加日志；
- SQLite：结构化请求记录，支持 report/export/dashboard；
- Prometheus：请求计数、延迟 histogram、token/cost/cache/fallback 等；
- OpenTelemetry：可选 OTLP gRPC，带 GenAI semantic conventions；
- Web dashboard：本地 FastAPI router + 静态 HTML/JS。

日志写入在后台线程池执行，shutdown 时 drain，见 `server.py:342-380`；指标、OTel、dashboard 分别见 `nadirclaw/metrics.py`、`nadirclaw/telemetry.py`、`nadirclaw/web_dashboard.py`。

### 7. 配置与部署层

- 环境变量 + `~/.nadirclaw/.env`；模型、供应商凭据、端口、优化、限流、预算等都可配置。
- `Dockerfile` 使用 `python:3.11-slim`，暴露 8856，健康检查 `/health`。
- `docker-compose.yml` 提供 NadirClaw + Ollama 双容器和持久化 `ollama_data`。
- Python 3.10+，主要依赖 FastAPI、Uvicorn、LiteLLM、sentence-transformers、NumPy、python-dotenv、Click、google-genai、sse-starlette；可选 dashboard/headroom/telemetry/trained extras。

## N-tier / 验证级联的实现现状

仓库同时存在一套更先进的 `Cascade`/`NTierCascade` + YAML tier profile：

- `nadirclaw/cascade.py:95-385`：cheap call → heuristic verifier → reject 后升级；verifier 异常 fail-open，连续 3 次异常触发 kill switch。
- `nadirclaw/cascade.py:399-817`：`NTierCascade` 根据连续 score 和 `TierProfile` 选择起始 tier，支持 adjacent/jump、max hops、rule engine、heuristic 或可选 DeBERTa verifier。
- `nadirclaw/tier_config/loader.py:1-167`：YAML profile TTL+mtime 热加载，解析失败保留 last-good/default。
- `nadirclaw/tier_config/profiles/n2_default.yaml`：cheap + strong；`n2_trained.yaml`：可选 `nadirclaw/cascade-verifier-v1`；`n3_legacy.yaml`：simple/medium/complex。
- `nadirclaw/cascade_rules/engine.py` + `cascade_rules/profiles/*.yaml`：基于 substring/regex/长度/分类置信度的 declarative `force_escalate`、`force_cheap`、`set_threshold`、`set_max_tokens`。

**重要边界**：截至当前 commit，`server.py` 的默认 `/v1/chat/completions` 路径只调用 `_smart_route_full()`、`apply_routing_modifiers()` 和 `_call_with_fallback()`；仓库内没有 `server.py -> load_profile()/NTierCascade` 的调用。也就是说：

- 这套 N-tier/后生成 verifier 是可复用库组件和测试覆盖的能力；
- 文档将它描述成 `serve` 默认请求链的一部分，但从静态调用关系看，默认 HTTP 服务仍是“预生成分类 + 故障 fallback”，不是“便宜模型生成后再验证并升级”；
- 若要在生产 HTTP 路径启用它，需要把 profile/tier callers、verifier、rule engine 与现有 provider dispatch 组合起来，并补齐流式场景的语义（后生成验证天然更适合非流式）。

这是本项目最值得关注的架构一致性风险，也是后续演进的首要切入点。

## 技术选型评价

### 合理之处

- **FastAPI + async I/O** 适合代理型服务；Gemini 同步 SDK 用有界线程池隔离，避免阻塞事件循环。
- **LiteLLM** 降低多供应商适配成本，同时保留 Gemini 原生路径以处理 SDK 能力和 OAuth/Vertex 特例。
- **轻量 centroid classifier** 冷启动和推理成本低，且模型不可用时可回退；训练模型作为可选增强，符合本地工具的安装体验。
- **模块化 fallback/health/rate-limit/cache/metrics** 对代理服务的可运维性有直接价值。
- **本地优先 + BYOK** 减少数据和密钥离开用户环境的范围，适合 coding-agent 场景。

### 风险与局限

- **两套路由架构并存**：legacy env-var smart routing 与 N-tier YAML/NTierCascade 的职责、默认入口和配置优先级不完全统一，容易造成“改了 profile 但 serve 行为没变”的误解。
- **后生成质量验证未接入默认 server**：README 的“Route → Verify → Escalate”与实际默认调用链存在差异；即使 `HeuristicVerifier` 只能发现明显拒答/截断/JSON 错误，这一差异也会影响质量和成本预期。
- **训练模型/验证器下载和可复现性**：DistilBERT、BGE、DeBERTa 依赖本地缓存或 Hugging Face，离线部署需要额外制品管理；训练 pipeline 未发布，模型漂移只能靠人工替换。
- **内存态状态**：session cache、prompt cache、provider health、request rate limiter 进程内有效，多 worker/多副本时不共享；要横向扩展需要外部状态或 sticky routing。
- **模型 registry 与真实供应商能力可能漂移**：context window、价格、vision 能力是代码内静态元数据，需依赖 `update-models`/本地 metadata 维护。
- **流式与验证天然冲突**：真实 SSE 是边生成边返回，无法在首字节前完成完整后生成质量判定；当前实现因此把 fallback 重点放在“调用失败”，而非“答案质量失败”。
- **安全默认仍偏 localhost**：若暴露到局域网/公网，需要主动配置 auth、CORS、HSTS、日志隐私和反向代理 TLS；README/CHANGELOG 已提示生产硬化要求。
- **许可证不是宽松开源**：`LICENSE` 和 `pyproject.toml:10-12` 明确为 PolyForm Noncommercial 1.0.0；商业部署/提供付费服务需要另行授权。

## 适用场景

- 个人开发者、coding agent、Open WebUI/Cursor/Continue/Aider/OpenClaw 等已有 OpenAI/Anthropic 客户端。
- 希望 BYOK、在本机统一接入 Gemini/OpenAI/Anthropic/Ollama，并降低简单请求成本的团队原型。
- 需要统一日志、预算、fallback 和本地 dashboard，但不想引入完整云端网关的环境。

不适合直接当作多租户生产 API gateway：鉴权、状态、计费、配置和观测都是单进程/本地优先设计；商业使用还受 PolyForm Noncommercial 限制。

## 研究限制与验证

- 已读取本地源码、README、配置、Docker/Compose、CHANGELOG、LICENSE 和测试目录。
- 仓库包含 47 个 `test_*.py` 文件、约 13k 行测试代码；`python3 -m compileall` 全量语法编译通过。本环境未安装 pytest 及项目依赖，因此没有执行运行时测试。
- 代码知识图谱 MCP 在本次会话因后端 503 自动审批失败，已按 AGENTS.md 的 fallback 规则改用 `rg`、`sed`、`nl` 做静态发现；结论以静态源码为准。
