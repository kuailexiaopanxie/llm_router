# Coding LLM Router

Local-first deterministic router for Anthropic Messages and OpenAI Responses clients.

## Run

```bash
cp router.example.yaml router.yaml
export LLM_ROUTER_CLIENT_API_KEY=local-client-token
export LLM_ROUTER_ANTHROPIC_API_KEY=upstream-token
export LLM_ROUTER_OPENAI_API_KEY=openai-upstream-token
python -m pip install -e .
llm-router --config router.yaml
```

The default listener is `http://127.0.0.1:8848`.

The protocol entry points are `POST /v1/messages` and `POST /v1/responses`.

v0.4 keeps Anthropic Messages and OpenAI Responses as separate same-protocol routes. The in-memory health coordinator filters targets in cooldown or blocked state, admits one recovery probe after cooldown, and preserves the configured capability and fallback order. Health is disabled with `health.enabled: false`; no Anthropic/OpenAI protocol conversion is performed.

The example configuration uses `health` defaults suitable for local operation. Health state is process-local and resets on restart. SQLite and metrics contain only bounded route and health observations; request bodies, response bodies, credentials, and reasoning content are not persisted.

## Outcome Feedback

Submit bounded evidence using the Router-owned request ID returned in `x-llm-router-request-id`:

```bash
curl http://127.0.0.1:8848/v1/router/outcomes \
  -H "authorization: Bearer $LLM_ROUTER_CLIENT_API_KEY" \
  -H 'content-type: application/json' \
  -d '{
    "event_id":"019d0000-0000-7000-8000-000000000001",
    "request_id":"019d0000-0000-7000-8000-000000000002",
    "verdict":"success",
    "evidence":"test",
    "source":"ci"
  }'
```

Outcome Events are append-only observations. They never update online session escalation, Provider Health, or routing policy.

## Offline Replay

```bash
llm-router replay \
  --db ~/.llm-router/router.db \
  --candidate-config router-candidate.yaml \
  --mode historical \
  --format table
```

Replay opens SQLite read-only, does not resolve Provider secrets, and never calls a Provider. It reports how candidate execution plans differ; historical Outcome data is not assigned to hypothetical targets and cannot establish candidate quality, success rate, cost, or latency.

## Online Shadow Policy

Shadow evaluation is disabled by default. Enable it with one fixed local candidate file:

```yaml
shadow:
  enabled: true
  candidate_config_path: ./router-candidate.yaml
  sample_rate: 0.10
  protocols: []
  profiles: []
  queue_capacity: 256
  evaluation_timeout_ms: 25
```

The candidate is compiled at startup without resolving its API keys or creating a Provider. Shadow evaluation is sampled, asynchronous, bounded, and fail-open; it never changes the actual model request. It compares only routing plans and normalized errors.

Inspect persisted comparisons without loading configuration or secrets:

```bash
llm-router shadow-report \
  --db ~/.llm-router/router.db \
  --format table \
  --limit 10000
```

The report describes structural changes and actual Outcome coverage only. It does not estimate candidate success rate, quality, cost, latency, or expected answers. Set `shadow.enabled: false` and restart to roll back shadow admission; retained shadow records remain readable.
