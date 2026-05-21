"""Factory für LLM-Backends — zentrale Instanziierung anhand der Konfiguration."""

from models.enums import LLMBackendType

from .anthropic_backend import AnthropicBackend
from .base import BaseLLMBackend, LLMBackendConfig
from .fallback_backend import FallbackOllamaBackend
from .ollama_backend import OllamaBackend
from .openai_compatible_backend import OpenAICompatibleBackend

# Backend-Typen die die OpenAI-kompatible API verwenden
_OPENAI_COMPATIBLE = {
    LLMBackendType.LM_STUDIO,
    LLMBackendType.VLLM,
    LLMBackendType.LOCAL_AI,
    LLMBackendType.TABBY_API,
    LLMBackendType.LLAMA_CPP,
    LLMBackendType.OPENAI,
    LLMBackendType.OPENROUTER,
    LLMBackendType.GEMINI,  # Gemini unterstützt OpenAI-kompatible API
}


def create_llm_backend(config: LLMBackendConfig) -> BaseLLMBackend:
    """
    Erzeugt das passende LLM-Backend für die gegebene Konfiguration.

    Wirft ValueError wenn das Backend deaktiviert oder unbekannt ist.
    """
    if not config.enabled:
        raise ValueError(f"LLM-Backend '{config.backend_type}' ist deaktiviert.")

    if config.backend_type == LLMBackendType.OLLAMA:
        primary = OllamaBackend(config)
        if config.fallback_base_url:
            fallback_config = LLMBackendConfig(
                backend_type=LLMBackendType.OLLAMA,
                model=config.fallback_model or config.model,
                base_url=config.fallback_base_url,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                timeout_seconds=config.timeout_seconds,
                max_retries=config.max_retries,
            )
            return FallbackOllamaBackend(primary, OllamaBackend(fallback_config))
        return primary

    if config.backend_type == LLMBackendType.ANTHROPIC:
        return AnthropicBackend(config)

    if config.backend_type in _OPENAI_COMPATIBLE:
        return OpenAICompatibleBackend(config)

    raise ValueError(f"Unbekannter LLM-Backend-Typ: {config.backend_type}")


def create_backend_from_env(backend_type: LLMBackendType) -> BaseLLMBackend:
    """Erzeugt ein Backend mit Standardkonfiguration aus Umgebungsvariablen."""
    import os

    model_env_key = f"LLM_{backend_type.upper()}_MODEL"
    api_key_env_key = f"LLM_{backend_type.upper()}_API_KEY"
    base_url_env_key = f"LLM_{backend_type.upper()}_BASE_URL"

    default_models: dict[LLMBackendType, str] = {
        LLMBackendType.OLLAMA: "qwen2.5-coder:14b",
        LLMBackendType.LM_STUDIO: "local-model",
        LLMBackendType.VLLM: "Qwen/Qwen2.5-Coder-14B-Instruct",
        LLMBackendType.ANTHROPIC: "claude-sonnet-4-6",
        LLMBackendType.OPENAI: "gpt-4o",
        LLMBackendType.OPENROUTER: "deepseek/deepseek-coder",
        LLMBackendType.LOCAL_AI: "local-model",
        LLMBackendType.TABBY_API: "local-model",
        LLMBackendType.LLAMA_CPP: "local-model",
        LLMBackendType.GEMINI: "gemini-2.0-flash",
    }

    config = LLMBackendConfig(
        backend_type=backend_type,
        model=os.getenv(model_env_key, default_models.get(backend_type, "local-model")),
        api_key=os.getenv(api_key_env_key),
        base_url=os.getenv(base_url_env_key),
    )
    return create_llm_backend(config)
