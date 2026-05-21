"""LLM-Backend-Abstraktion für lokale und Cloud-Modelle."""

from .anthropic_backend import AnthropicBackend
from .base import BaseLLMBackend, LLMBackendConfig, LLMMessage, LLMResponse
from .factory import create_backend_from_env, create_llm_backend
from .ollama_backend import OllamaBackend
from .openai_compatible_backend import OpenAICompatibleBackend

__all__ = [
    "AnthropicBackend",
    "BaseLLMBackend",
    "LLMBackendConfig",
    "LLMMessage",
    "LLMResponse",
    "OllamaBackend",
    "OpenAICompatibleBackend",
    "create_backend_from_env",
    "create_llm_backend",
]
