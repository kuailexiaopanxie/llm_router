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

## Local Dashboard

v0.8 includes a local, read-only observability dashboard. It is disabled by default. Enable it in `router.yaml`:

```yaml
dashboard:
  enabled: true
  require_auth: true
```

Then open `http://127.0.0.1:8848/admin`. The JSON API is under `/admin/api/v1/overview`, `/admin/api/v1/requests`, and `/admin/api/v1/requests/<request-uuid>`. JSON requests use the existing client key as `Authorization: Bearer ...`; the browser keeps that key in memory only and asks again after reload. A loopback-only deployment may explicitly set `require_auth: false`. Remote listeners reject unauthenticated Dashboard configuration.

The Dashboard is GET/HEAD-only. It reads persisted SQLite observations and a separately labelled live runtime snapshot; it does not edit configuration, route traffic, call Providers, or create observations. It never displays Prompt, Response, Reasoning content, source, tools, session identifiers, credentials, arbitrary headers, or exception text. Unknown and partial facts remain visible as gaps. Cost is labelled `Known estimated cost`, uses decimal nanos strings, and never combines currencies or claims to be an invoice. Missing SQLite history, disabled capture, legacy rows, unavailable trace, and query timeout are shown as distinct states.

v0.4 keeps Anthropic Messages and OpenAI Responses as separate same-protocol routes. The in-memory health coordinator filters targets in cooldown or blocked state, admits one recovery probe after cooldown, and preserves the configured capability and fallback order. Health is disabled with `health.enabled: false`; no Anthropic/OpenAI protocol conversion is performed.

The example configuration uses `health` defaults suitable for local operation. Health state is process-local and resets on restart. SQLite, metrics, and traces contain only bounded observations; request bodies, response bodies, credentials, session identifiers, tool arguments, and reasoning content are not persisted.

Every model response includes Router-owned `x-llm-router-request-id` and `x-llm-router-trace-id` headers. A terminal observation correlates the actual policy, selected and final targets, attempts, latency, Provider-reported usage, known estimated cost, and optional local trace spans. Observability sinks are fail-open and do not change routing or Provider requests.

## Observability Queries

The v0.7 query commands open SQLite read-only and do not load Router YAML, API keys, or Provider clients:

```bash
llm-router routes --db ~/.llm-router/router.db --last 30
llm-router trace --db ~/.llm-router/router.db --request <request-uuid>
llm-router cost --db ~/.llm-router/router.db --today --group-by model --format json
```

Cost is calculated only from Provider-reported usage and versioned Decimal rates configured on each model. SQLite stores integer nanos and the historical rate snapshot. `complete`, `partial`, `unpriced`, `usage_missing`, and `not_applicable` coverage remain separate; a known partial amount is never presented as total cost or as a Provider invoice. Different currencies are never combined.

Local trace storage is enabled by default. OTLP/HTTP export is optional, uses an independent bounded queue, and is not a readiness dependency. Configure credentials through `observability.tracing.otlp.headers_env`; header values are never recorded. `observability.retention_days` deletes only expired observation tables in batches and leaves Outcome, Replay, Shadow, and Canary evaluation data untouched.

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

## Controlled Canary Routing

v0.6 can route a bounded cohort of eligible requests through one reviewed Candidate policy. Assignment is stable HMAC-based affinity using session, task, then request identity. Each request selects exactly one policy and invokes the existing execution engine at most once; a Candidate routing or Provider failure never falls back across policies to Current.

The operational sequence is manual and restart-bound:

1. Run Shadow with the fixed Candidate until every declared segment meets its persisted gate.
2. Set `candidate_policy.expected_policy_hash`, provide a secret salt of at least 32 bytes through `LLM_ROUTER_CANARY_SALT`, enable Canary at a small rate, and restart.
3. Inspect actual observations with `llm-router canary-report --db ~/.llm-router/router.db --format table`.
4. Increase the rate, promote by replacing Current, or roll back with `canary.enabled: false`; every change requires restart.

The startup gate checks replayability and known evaluation failures, not quality or causality. `canary-report` opens SQLite read-only and reports explicit denominators, completion gaps, Outcome coverage, and conflicts. It does not compute uplift, statistical significance, or an automatic promotion decision. Stateful response requests without session/task affinity and count-token requests remain on Current.
