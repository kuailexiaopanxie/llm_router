"""Provider abstraction used by the execution engine."""

from __future__ import annotations

from typing import Protocol

from llm_router.domain import ProviderExchange, ProviderRequest


class ProviderPort(Protocol):
    """Open one upstream protocol exchange for a resolved model target."""

    async def invoke(self, request: ProviderRequest) -> ProviderExchange:
        """Open an upstream exchange and expose status, headers, and raw bytes."""

