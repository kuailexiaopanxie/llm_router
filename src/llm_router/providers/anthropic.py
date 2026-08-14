"""Direct httpx adapter for the Anthropic Messages API."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx

from llm_router.config import ProviderConfig
from llm_router.domain import ProviderExchange, ProviderRequest
from llm_router.gateway.errors import RouterError


_FORWARDED_HEADERS = {
    "anthropic-version",
    "anthropic-beta",
    "content-type",
    "accept",
}
class AnthropicProvider:
    """Provider adapter that preserves unknown JSON fields and SSE bytes."""

    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        self._config = config
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=config.base_url.rstrip("/"))
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    def _headers(self, incoming: Mapping[str, str]) -> dict[str, str]:
        """Filter client headers and inject upstream authentication."""

        allowed = _FORWARDED_HEADERS | {item.lower() for item in self._config.extension_headers}
        headers = {key: value for key, value in incoming.items() if key.lower() in allowed}
        headers.pop("authorization", None)
        headers.pop("x-api-key", None)
        if self._config.auth_scheme == "bearer":
            headers["authorization"] = f"Bearer {self._api_key}"
        else:
            headers["x-api-key"] = self._api_key
        headers.setdefault("content-type", "application/json")
        return headers

    async def invoke(self, request: ProviderRequest) -> ProviderExchange:
        """Open one request while bounding provider concurrency."""

        await self._semaphore.acquire()
        body = dict(request.envelope.raw_body)
        body["model"] = request.target.upstream_model
        timeout = httpx.Timeout(
            timeout=None,
            connect=request.connect_timeout,
            read=None,
            write=request.connect_timeout,
            pool=request.connect_timeout,
        )
        try:
            outbound = self._client.build_request(
                "POST",
                "/v1/messages/count_tokens" if request.envelope.endpoint.endswith("count_tokens") else "/v1/messages",
                headers=self._headers(request.envelope.safe_headers),
                json=body,
                timeout=timeout,
            )
            response = await self._client.send(outbound, stream=True)
        except (httpx.HTTPError, OSError) as exc:
            self._semaphore.release()
            raise RouterError(
                "router_upstream_connect_failed",
                503,
                "The upstream connection failed.",
                "overloaded_error",
                fallback_allowed=True,
            ) from exc

        async def close() -> None:
            """Close the response and release the provider permit exactly once."""

            try:
                await response.aclose()
            finally:
                self._semaphore.release()

        return ProviderExchange(
            status_code=response.status_code,
            headers={key.lower(): value for key, value in response.headers.items()},
            body=response.aiter_raw(),
            close=close,
        )

    async def close(self) -> None:
        """Close the shared HTTP client during application shutdown."""

        await self._client.aclose()


class ProviderRegistry:
    """Resolve configured provider names without exposing provider details to routing."""

    def __init__(self, providers: Mapping[str, ProviderConfig], keys: Mapping[str, str]) -> None:
        self._providers = {
            name: AnthropicProvider(config, keys[name]) for name, config in providers.items()
        }

    def get(self, name: str) -> AnthropicProvider:
        """Return a configured provider adapter by name."""

        return self._providers[name]

    async def close(self) -> None:
        """Close all provider HTTP clients."""

        await asyncio.gather(*(provider.close() for provider in self._providers.values()))
