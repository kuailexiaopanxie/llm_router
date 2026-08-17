"""Direct httpx adapter for the OpenAI Responses API."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

import httpx

from llm_router.config import ProviderConfig
from llm_router.domain import ProviderExchange, ProviderRequest
from llm_router.gateway.errors import RouterError


_FORWARDED_HEADERS = {"content-type", "accept"}


class OpenAIProvider:
    """Provider adapter that preserves Responses JSON fields and SSE bytes."""

    def __init__(self, config: ProviderConfig, api_key: str) -> None:
        """Initialize one OpenAI upstream connection pool."""

        self._config = config
        self._api_key = api_key
        self._client = httpx.AsyncClient(base_url=config.base_url.rstrip("/"))
        self._semaphore = asyncio.Semaphore(config.max_concurrency)

    def _headers(self, incoming: Mapping[str, str]) -> dict[str, str]:
        """Filter client headers and inject provider Bearer authentication."""

        allowed = _FORWARDED_HEADERS | {item.lower() for item in self._config.extension_headers}
        headers = {key: value for key, value in incoming.items() if key.lower() in allowed}
        headers.pop("authorization", None)
        headers.pop("x-api-key", None)
        headers["authorization"] = f"Bearer {self._api_key}"
        headers.setdefault("content-type", "application/json")
        return headers

    async def invoke(self, request: ProviderRequest) -> ProviderExchange:
        """Open one Responses request while bounding provider concurrency."""

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
                "/v1/responses",
                headers=self._headers(request.envelope.safe_headers),
                json=body,
                timeout=timeout,
            )
            response = await self._client.send(outbound, stream=True)
        except asyncio.CancelledError:
            self._semaphore.release()
            raise
        except (httpx.HTTPError, OSError) as exc:
            self._semaphore.release()
            raise RouterError(
                "router_upstream_connect_failed",
                503,
                "The upstream connection failed.",
                fallback_allowed=True,
            ) from exc

        closed = False

        async def close() -> None:
            """Close the response and release the provider permit exactly once."""

            nonlocal closed
            if closed:
                return
            closed = True
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
