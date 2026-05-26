"""Fallback-Backend: versucht primären Ollama-Server, weicht auf Backup aus."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from .base import BaseLLMBackend, LLMBackendConfig, LLMMessage, LLMResponse
from .ollama_backend import OllamaBackend

logger = logging.getLogger(__name__)


class FallbackOllamaBackend(BaseLLMBackend):
    """
    Wrapper um zwei Ollama-Server: primär und Fallback.

    Bei ConnectError oder RuntimeError auf dem primären Server wird
    automatisch der Fallback-Server versucht. Ist
    fallback_first_token_timeout_seconds > 0, wird der primäre Aufruf
    nach dieser Zeit abgebrochen und der Fallback gestartet, statt den
    vollen timeout_seconds abzuwarten.
    """

    def __init__(self, primary: OllamaBackend, fallback: OllamaBackend) -> None:
        super().__init__(primary.config)
        self._primary = primary
        self._fallback = fallback

    @property
    def _first_token_timeout(self) -> float | None:
        t = self.config.fallback_first_token_timeout_seconds
        return float(t) if t > 0 else None

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        try:
            coro = self._primary.complete(messages, system_prompt=system_prompt, tools=tools)
            timeout = self._first_token_timeout
            if timeout is not None:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro
        except asyncio.TimeoutError:
            logger.warning(
                "Primärer LLM-Server hat nach %.0fs nicht geantwortet, versuche Fallback",
                self._first_token_timeout,
            )
            return await self._fallback.complete(messages, system_prompt=system_prompt, tools=tools)
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
            timeout = self._first_token_timeout
            if timeout is not None:
                # Ersten Chunk mit Timeout abwarten, dann den Rest normal streamen
                gen = self._primary.stream(messages, system_prompt=system_prompt)
                first = await asyncio.wait_for(gen.__anext__(), timeout=timeout)
                yield first
                async for chunk in gen:
                    yield chunk
            else:
                async for chunk in self._primary.stream(messages, system_prompt=system_prompt):
                    yield chunk
        except asyncio.TimeoutError:
            logger.warning(
                "Primärer LLM-Stream hat nach %.0fs keinen ersten Chunk geliefert, versuche Fallback",
                self._first_token_timeout,
            )
            async for chunk in self._fallback.stream(messages, system_prompt=system_prompt):
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
