"""Abstrakte Basisklasse für alle LLM-Backends."""

from abc import ABC, abstractmethod
from typing import Any, AsyncIterator

from pydantic import BaseModel, Field

from models.enums import LLMBackendType


class LLMMessage(BaseModel):
    """Einzelne Nachricht im Chat-Format."""

    role: str  # system | user | assistant | tool
    content: str
    name: str | None = None


class LLMResponse(BaseModel):
    """Normalisierte Antwort eines LLM-Backends."""

    content: str
    model: str
    backend: LLMBackendType
    finish_reason: str | None = None

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    raw: dict[str, Any] = Field(default_factory=dict)


class LLMBackendConfig(BaseModel):
    """Konfiguration für ein LLM-Backend."""

    backend_type: LLMBackendType
    model: str
    base_url: str | None = None
    api_key: str | None = None

    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)
    max_retries: int = Field(default=3, ge=0)

    # Datenschutz: sensible Daten anonymisieren bei Cloud-Backends
    anonymize_sensitive_data: bool = False
    enabled: bool = True

    # Optionaler Fallback-Server (nur für Ollama)
    fallback_base_url: str | None = None
    fallback_model: str | None = None
    # Wie viele Sekunden auf die erste Antwort des primären Servers gewartet wird,
    # bevor auf den Fallback umgeschaltet wird (0 = kein separates First-Token-Timeout)
    fallback_first_token_timeout_seconds: int = Field(default=0, ge=0, le=300)


class BaseLLMBackend(ABC):
    """
    Abstrakte Basisklasse für LLM-Backends.

    Alle Backends implementieren ein einheitliches async-Interface, sodass
    Agenten vollständig backend-agnostisch implementiert werden können.
    """

    def __init__(self, config: LLMBackendConfig) -> None:
        self.config = config

    @property
    def backend_type(self) -> LLMBackendType:
        return self.config.backend_type

    @property
    def is_local(self) -> bool:
        """Gibt an ob dieses Backend lokal läuft (kein Cloud-Zugriff)."""
        return self.config.backend_type not in {
            LLMBackendType.ANTHROPIC,
            LLMBackendType.OPENAI,
            LLMBackendType.GEMINI,
            LLMBackendType.OPENROUTER,
        }

    @abstractmethod
    async def complete(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> LLMResponse:
        """Sendet eine Chat-Anfrage und gibt die vollständige Antwort zurück."""

    @abstractmethod
    async def stream(
        self,
        messages: list[LLMMessage],
        *,
        system_prompt: str | None = None,
    ) -> AsyncIterator[str]:
        """Streamt die Antwort als AsyncIterator von Text-Chunks."""

    @abstractmethod
    async def health_check(self) -> bool:
        """Prüft ob das Backend erreichbar und betriebsbereit ist."""

    async def __aenter__(self) -> "BaseLLMBackend":
        return self

    async def __aexit__(self, *_: Any) -> None:
        pass
