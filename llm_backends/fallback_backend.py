"""Fallback-Backend: versucht primären Ollama-Server, weicht auf Backup aus.

Sobald der primäre Server ausfällt, startet ein Background-Task der ihn
alle PRIMARY_PROBE_INTERVAL_SECONDS anpingt. Antwortet er wieder, wird
sofort auf ihn zurückgeschaltet — alle Anfragen in der Zwischenzeit gehen
an den Fallback-Server.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator

from .base import BaseLLMBackend, LLMMessage, LLMResponse
from .ollama_backend import OllamaBackend

logger = logging.getLogger(__name__)

PRIMARY_PROBE_INTERVAL_SECONDS = 60


class FallbackOllamaBackend(BaseLLMBackend):
    """
    Wrapper um zwei Ollama-Server: primär und Fallback.

    Normalbetrieb: alle Anfragen gehen an den primären Server.

    Bei Ausfall:
    - Anfragen werden sofort an den Fallback weitergeleitet.
    - Ein Background-Task prüft alle PRIMARY_PROBE_INTERVAL_SECONDS ob der
      primäre Server wieder erreichbar ist.
    - Sobald der primäre Server antwortet, wird auf ihn zurückgeschaltet
      und der Probe-Task beendet.

    Ist fallback_first_token_timeout_seconds > 0, wird der primäre Aufruf
    nach dieser Zeit abgebrochen (statt den vollen timeout_seconds abzuwarten)
    bevor der Fallback greift.
    """

    def __init__(self, primary: OllamaBackend, fallback: OllamaBackend) -> None:
        super().__init__(primary.config)
        self._primary = primary
        self._fallback = fallback
        self._primary_healthy = True
        self._probe_task: asyncio.Task | None = None

    @property
    def _first_token_timeout(self) -> float | None:
        t = self.config.fallback_first_token_timeout_seconds
        return float(t) if t > 0 else None

    # ------------------------------------------------------------------
    # Primär-Ausfall / Wiederherstellung
    # ------------------------------------------------------------------

    def _mark_primary_failed(self) -> None:
        """Schaltet auf Fallback und startet den Hintergrund-Probe falls nötig."""
        if self._primary_healthy:
            self._primary_healthy = False
            logger.warning(
                "Primärer LLM-Server nicht erreichbar — wechsle auf Fallback. "
                "Prüfe alle %ds ob er wieder verfügbar ist.",
                PRIMARY_PROBE_INTERVAL_SECONDS,
            )
        self._ensure_probe_running()

    def _mark_primary_recovered(self) -> None:
        """Schaltet zurück auf den primären Server."""
        self._primary_healthy = True
        logger.info(
            "Primärer LLM-Server wieder erreichbar — wechsle zurück auf primären Server."
        )

    def _ensure_probe_running(self) -> None:
        """Startet den Probe-Task falls er noch nicht läuft."""
        if self._probe_task is None or self._probe_task.done():
            try:
                loop = asyncio.get_event_loop()
                self._probe_task = loop.create_task(self._probe_primary())
            except RuntimeError:
                pass  # kein laufender Event-Loop (z.B. in Tests)

    async def _probe_primary(self) -> None:
        """Prüft periodisch ob der primäre Server wieder antwortet."""
        while not self._primary_healthy:
            await asyncio.sleep(PRIMARY_PROBE_INTERVAL_SECONDS)
            try:
                ok = await self._primary.health_check()
                if ok:
                    self._mark_primary_recovered()
                else:
                    logger.debug(
                        "Primärer LLM-Server noch nicht erreichbar — nächste Prüfung in %ds.",
                        PRIMARY_PROBE_INTERVAL_SECONDS,
                    )
            except Exception as exc:
                logger.debug("Primär-Probe fehlgeschlagen: %s", exc)

    # ------------------------------------------------------------------
    # LLM-Aufrufe
    # ------------------------------------------------------------------

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        if not self._primary_healthy:
            return await self._fallback.complete(
                messages, system_prompt=system_prompt, tools=tools
            )

        try:
            coro = self._primary.complete(messages, system_prompt=system_prompt, tools=tools)
            timeout = self._first_token_timeout
            if timeout is not None:
                return await asyncio.wait_for(coro, timeout=timeout)
            return await coro
        except asyncio.TimeoutError:
            logger.warning(
                "Primärer LLM-Server hat nach %.0fs nicht geantwortet, versuche Fallback.",
                self._first_token_timeout,
            )
            self._mark_primary_failed()
            return await self._fallback.complete(
                messages, system_prompt=system_prompt, tools=tools
            )
        except Exception as exc:
            # _mark_primary_failed() loggt die Warnung selbst (nur beim ersten Ausfall)
            self._mark_primary_failed()
            logger.debug("Primärer LLM-Server Fehlerdetail: %s", exc)
            return await self._fallback.complete(
                messages, system_prompt=system_prompt, tools=tools
            )

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        if not self._primary_healthy:
            async for chunk in self._fallback.stream(messages, system_prompt=system_prompt):
                yield chunk
            return

        try:
            timeout = self._first_token_timeout
            if timeout is not None:
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
                "Primärer LLM-Stream hat nach %.0fs keinen ersten Chunk geliefert, "
                "versuche Fallback.",
                self._first_token_timeout,
            )
            self._mark_primary_failed()
            async for chunk in self._fallback.stream(messages, system_prompt=system_prompt):
                yield chunk
        except Exception as exc:
            self._mark_primary_failed()
            logger.debug("Primärer LLM-Stream Fehlerdetail: %s", exc)
            async for chunk in self._fallback.stream(messages, system_prompt=system_prompt):
                yield chunk

    async def health_check(self) -> bool:
        if await self._primary.health_check():
            if not self._primary_healthy:
                self._mark_primary_recovered()
            return True
        return await self._fallback.health_check()
