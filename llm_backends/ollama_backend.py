"""Ollama-Backend für lokale LLM-Ausführung."""

import asyncio
from typing import Any, AsyncIterator

import httpx

from models.enums import LLMBackendType

from .base import BaseLLMBackend, LLMBackendConfig, LLMMessage, LLMResponse


class OllamaBackend(BaseLLMBackend):
    """
    Backend für Ollama — der bevorzugte lokale LLM-Runner.

    Unterstützt DeepSeek, Qwen, Llama, Mistral, Codestral, Devstral,
    Granite, Phi, Gemma und alle weiteren Ollama-kompatiblen Modelle.
    """

    DEFAULT_BASE_URL = "http://localhost:11434"

    def __init__(self, config: LLMBackendConfig) -> None:
        super().__init__(config)
        self._base_url = config.base_url or self.DEFAULT_BASE_URL
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            # connect: 10s Wartezeit für erste Verbindung (Modell lädt ggf. noch)
            # read:    timeout_seconds für die eigentliche Antwort (lange Generierung)
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=httpx.Timeout(
                    connect=10.0,
                    read=float(self.config.timeout_seconds),
                    write=30.0,
                    pool=5.0,
                ),
            )
        return self._client

    def _build_payload(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None,
        stream: bool,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        all_messages: list[dict[str, Any]] = []
        if system_prompt:
            all_messages.append({"role": "system", "content": system_prompt})
        all_messages.extend({"role": m.role, "content": m.content} for m in messages)

        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": all_messages,
            "stream": stream,
            "options": {
                "temperature": self.config.temperature,
                "num_predict": self.config.max_tokens,
            },
        }
        if tools:
            payload["tools"] = tools
        return payload

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        client = self._get_client()
        payload = self._build_payload(messages, system_prompt=system_prompt, stream=False, tools=tools)

        for attempt in range(self.config.max_retries + 1):
            try:
                response = await client.post("/api/chat", json=payload)
                response.raise_for_status()
                data = response.json()
                msg = data.get("message", {})
                return LLMResponse(
                    content=msg.get("content", ""),
                    model=data.get("model", self.config.model),
                    backend=LLMBackendType.OLLAMA,
                    finish_reason=data.get("done_reason"),
                    prompt_tokens=data.get("prompt_eval_count", 0),
                    completion_tokens=data.get("eval_count", 0),
                    total_tokens=data.get("prompt_eval_count", 0) + data.get("eval_count", 0),
                    raw=data,
                )
            except (httpx.HTTPError, httpx.ConnectError) as exc:
                if attempt == self.config.max_retries:
                    raise RuntimeError(f"Ollama backend failed after {attempt + 1} attempts: {exc}") from exc
                await asyncio.sleep(2**attempt)

        raise RuntimeError("Unreachable")  # noqa: EM101

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        client = self._get_client()
        payload = self._build_payload(messages, system_prompt=system_prompt, stream=True)

        async with client.stream("POST", "/api/chat", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line:
                    continue
                import json
                data = json.loads(line)
                chunk = data.get("message", {}).get("content", "")
                if chunk:
                    yield chunk

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            response = await client.get("/api/tags")
            return response.status_code == 200
        except Exception:
            return False

    async def __aenter__(self) -> "OllamaBackend":
        return self

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
