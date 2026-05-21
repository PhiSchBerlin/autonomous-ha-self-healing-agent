"""Fallback-Backend: versucht primären Ollama-Server, weicht auf Backup aus."""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator

from .base import BaseLLMBackend, LLMBackendConfig, LLMMessage, LLMResponse
from .ollama_backend import OllamaBackend

logger = logging.getLogger(__name__)


class FallbackOllamaBackend(BaseLLMBackend):
    """
    Wrapper um zwei Ollama-Server: primär und Fallback.

    Bei ConnectError oder RuntimeError auf dem primären Server wird
    automatisch der Fallback-Server versucht.
    """

    def __init__(self, primary: OllamaBackend, fallback: OllamaBackend) -> None:
        super().__init__(primary.config)
        self._primary = primary
        self._fallback = fallback

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        try:
            return await self._primary.complete(messages, system_prompt=system_prompt, tools=tools)
        except Exception as exc:
            logger.warning("Primärer LLM-Server nicht erreichbar (%s), versuche Fallback", exc)
            return await self._fallback.complete(messages, system_prompt=system_prompt, tools=tools)

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        try:
            async for chunk in self._primary.stream(messages, system_prompt=system_prompt):
                yield chunk
        except Exception as exc:
            logger.warning("Primärer LLM-Stream fehlgeschlagen (%s), versuche Fallback", exc)
            async for chunk in self._fallback.stream(messages, system_prompt=system_prompt):
                yield chunk

    async def health_check(self) -> bool:
        primary_ok = await self._primary.health_check()
        if primary_ok:
            return True
        return await self._fallback.health_check()
