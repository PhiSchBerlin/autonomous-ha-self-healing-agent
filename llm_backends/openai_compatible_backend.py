"""
OpenAI-kompatibles Backend für LM Studio, vLLM, LocalAI, TabbyAPI.

Alle diese Backends implementieren die OpenAI Chat Completions API,
sodass sie mit einem einzigen Backend-Typ abgedeckt werden können.
"""

import asyncio
from typing import Any, AsyncIterator

import httpx

from models.enums import LLMBackendType

from .base import BaseLLMBackend, LLMBackendConfig, LLMMessage, LLMResponse

# Bekannte Standard-Ports pro Backend-Typ
KNOWN_BASE_URLS: dict[LLMBackendType, str] = {
    LLMBackendType.LM_STUDIO: "http://localhost:1234/v1",
    LLMBackendType.VLLM: "http://localhost:8000/v1",
    LLMBackendType.LOCAL_AI: "http://localhost:8080/v1",
    LLMBackendType.TABBY_API: "http://localhost:5000/v1",
    LLMBackendType.LLAMA_CPP: "http://localhost:8080/v1",
}


class OpenAICompatibleBackend(BaseLLMBackend):
    """
    Generisches OpenAI-kompatibles Backend.

    Verwendet für: LM Studio, vLLM, LocalAI, TabbyAPI, llama.cpp server.
    Kann auch für OpenAI und OpenRouter selbst verwendet werden.
    """

    def __init__(self, config: LLMBackendConfig) -> None:
        super().__init__(config)
        self._base_url = config.base_url or KNOWN_BASE_URLS.get(
            config.backend_type, "http://localhost:8000/v1"
        )
        self._headers: dict[str, str] = {"Content-Type": "application/json"}
        if config.api_key:
            self._headers["Authorization"] = f"Bearer {config.api_key}"
        self._client: httpx.AsyncClient | None = None

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                headers=self._headers,
                timeout=self.config.timeout_seconds,
            )
        return self._client

    def _build_messages(
        self, messages: list[LLMMessage], system_prompt: str | None
    ) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        if system_prompt:
            result.append({"role": "system", "content": system_prompt})
        result.extend({"role": m.role, "content": m.content} for m in messages)
        return result

    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        client = self._get_client()
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._build_messages(messages, system_prompt),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": False,
        }
        if tools:
            payload["tools"] = tools

        for attempt in range(self.config.max_retries + 1):
            try:
                response = await client.post("/chat/completions", json=payload)
                if response.status_code == 429:
                    retry_after = float(response.headers.get("Retry-After", 2 ** attempt))
                    await asyncio.sleep(retry_after)
                    continue
                response.raise_for_status()
                data = response.json()
                choice = data["choices"][0]
                usage = data.get("usage", {})
                return LLMResponse(
                    content=choice["message"]["content"] or "",
                    model=data.get("model", self.config.model),
                    backend=self.config.backend_type,
                    finish_reason=choice.get("finish_reason"),
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                    total_tokens=usage.get("total_tokens", 0),
                    raw=data,
                )
            except (httpx.HTTPError, KeyError) as exc:
                if attempt == self.config.max_retries:
                    raise RuntimeError(
                        f"{self.config.backend_type} backend failed after {attempt + 1} attempts: {exc}"
                    ) from exc
                await asyncio.sleep(2**attempt)

        raise RuntimeError("Unreachable")  # noqa: EM101

    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        import json

        client = self._get_client()
        payload: dict[str, Any] = {
            "model": self.config.model,
            "messages": self._build_messages(messages, system_prompt),
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
            "stream": True,
        }

        async with client.stream("POST", "/chat/completions", json=payload) as response:
            response.raise_for_status()
            async for line in response.aiter_lines():
                if not line or line == "data: [DONE]":
                    continue
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    delta = data["choices"][0].get("delta", {})
                    chunk = delta.get("content", "")
                    if chunk:
                        yield chunk

    async def health_check(self) -> bool:
        try:
            client = self._get_client()
            response = await client.get("/models")
            return response.status_code == 200
        except Exception:
            return False

    async def __aexit__(self, *args: Any) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
