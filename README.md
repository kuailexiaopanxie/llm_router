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
