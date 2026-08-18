"""Provider adapter registry assembled from validated configuration."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping

from llm_router.config import ProviderConfig
from llm_router.providers.anthropic import AnthropicProvider
from llm_router.providers.openai import OpenAIProvider
from llm_router.providers.port import ProviderPort

ProviderAdapter = AnthropicProvider | OpenAIProvider


class ProviderRegistry:
    """Resolve configured providers through the stable ProviderPort interface."""

    def __init__(self, providers: Mapping[str, ProviderConfig], keys: Mapping[str, str]) -> None:
        """Build one adapter for each validated provider configuration."""

        self._providers: dict[str, ProviderAdapter] = {
            name: self._build(config, keys[name]) for name, config in providers.items()
        }

    @staticmethod
    def _build(config: ProviderConfig, api_key: str) -> ProviderAdapter:
        """Construct the adapter selected by the validated provider type."""

        if config.type == "openai":
            return OpenAIProvider(config, api_key)
        return AnthropicProvider(config, api_key)

    def get(self, name: str) -> ProviderPort:
        """Return a configured provider adapter without exposing its implementation."""

        return self._providers[name]

    async def close(self) -> None:
        """Close all provider HTTP clients."""

        await asyncio.gather(*(provider.close() for provider in self._providers.values()))
