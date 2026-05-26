"""Anthropic Claude Backend (Cloud, optional)."""

import asyncio
from typing import Any, AsyncIterator

from models.enums import LLMBackendType

from .base import BaseLLMBackend, LLMBackendConfig, LLMMessage, LLMResponse


class AnthropicBackend(BaseLLMBackend):
    """
    Cloud-Backend für Anthropic Claude (claude-sonnet-4-6, claude-opus-4-7 usw.).

    Nur aktiv wenn explizit konfiguriert. Anonymisiert auf Wunsch sensible Daten
    vor dem Versand (anonymize_sensitive_data=True in der Config).
    """

    def __init__(self, config: LLMBackendConfig) -> None:
        super().__init__(config)
        self._client: Any = None

    def _get_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise ImportError(
                    "anthropic package nicht installiert. "
                    "Installation: pip install anthropic"
                ) from exc
            self._client = anthropic.AsyncAnthropic(
                api_key=self.config.api_key,
                timeout=self.config.timeout_seconds,
                max_retries=self.config.max_retries,
            )
        return self._client

    def _maybe_anonymize(self, text: str) -> str:
        """Einfache Anonymisierung für sensible Daten bei Cloud-Nutzung."""
        if not self.config.anonymize_sensitive_data:
            return text
        import re
        # IP-Adressen maskieren
        text = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "[IP_REDACTED]", text)
        # Token-ähnliche Strings (>20 alphanummerische Zeichen) maskieren
        text = re.sub(r"\b[A-Za-z0-9_\-]{20,}\b", "[TOKEN_REDACTED]", text)
        return text

    def _prepare_messages(
        self, messages: list[LLMMessage]
    ) -> list[dict[str, Any]]:
        return [
            {
                "role": m.role,
                "content": self._maybe_anonymize(m.content),
            }
            for m in messages
            if m.role != "system"
        ]

    def _get_system(self, messages: list[LLMMessage], system_prompt: str | None) -> str | None:
        system_msgs = [m.content for m in messages if m.role == "system"]
        parts = []
        if system_prompt:
            parts.append(self._maybe_anonymize(system_prompt))
        parts.extend(self._maybe_anonymize(s) for s in system_msgs)
        return "\n\n".join(parts) if parts else None

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        client = self._get_client()
        system = self._get_system(messages, system_prompt)
        anthropic_messages = self._prepare_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": anthropic_messages,
            "temperature": self.config.temperature,
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = tools

        for attempt in range(self.config.max_retries + 1):
            try:
                response = await client.messages.create(**kwargs)
                content_block = response.content[0]
                text = content_block.text if hasattr(content_block, "text") else ""
                return LLMResponse(
                    content=text,
                    model=response.model,
                    backend=LLMBackendType.ANTHROPIC,
                    finish_reason=response.stop_reason,
                    prompt_tokens=response.usage.input_tokens,
                    completion_tokens=response.usage.output_tokens,
                    total_tokens=response.usage.input_tokens + response.usage.output_tokens,
                    raw=response.model_dump(),
                )
            except Exception as exc:
                # Anthropic SDK wirft RateLimitError bei 429 — Retry-after respektieren
                try:
                    import anthropic as _anthropic
                    if isinstance(exc, _anthropic.RateLimitError):
                        retry_after = float(
                            getattr(exc, "response", None) and
                            exc.response.headers.get("retry-after", 2 ** attempt)
                            or 2 ** attempt
                        )
                        await asyncio.sleep(retry_after)
                        continue
                except ImportError:
                    pass
                if attempt == self.config.max_retries:
                    raise RuntimeError(f"Anthropic backend failed: {exc}") from exc
                await asyncio.sleep(2**attempt)

        raise RuntimeError("Unreachable")  # noqa: EM101

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        client = self._get_client()
        system = self._get_system(messages, system_prompt)
        anthropic_messages = self._prepare_messages(messages)

        kwargs: dict[str, Any] = {
            "model": self.config.model,
            "max_tokens": self.config.max_tokens,
            "messages": anthropic_messages,
            "temperature": self.config.temperature,
        }
        if system:
            kwargs["system"] = system

        async with client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                yield text

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            await client.messages.create(
                model=self.config.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "ping"}],
            )
            return True
        except Exception:
            return False

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.close()
            self._client = None
