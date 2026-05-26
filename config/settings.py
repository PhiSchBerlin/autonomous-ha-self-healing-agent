"""
Zentrales Konfigurationssystem mit Pydantic v2 Settings.

Alle Einstellungen können über Umgebungsvariablen oder eine .env-Datei
überschrieben werden. Sensible Felder werden nie geloggt.

Architektur: Jede Sub-Settings-Klasse ist eine eigenständige BaseSettings-Instanz
mit ihrem eigenen env_prefix. AppSettings aggregiert sie nach der Konstruktion,
sodass jedes Prefix korrekt aufgelöst wird (pydantic-settings v2 Muster).
"""

from dataclasses import dataclass, field
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from models.enums import AgentMode, LLMBackendType


class LLMSettings(BaseSettings):
    """Konfiguration für das primäre LLM-Backend."""

    model_config = SettingsConfigDict(env_prefix="LLM_", env_file=".env", extra="ignore")

    backend_type: LLMBackendType = LLMBackendType.OLLAMA
    model: str = "qwen2.5-coder:14b"
    base_url: str | None = None
    api_key: SecretStr | None = None

    temperature: float = Field(default=0.1, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1)
    timeout_seconds: int = Field(default=120, ge=1)
    max_retries: int = Field(default=1, ge=0)

    anonymize_sensitive_data: bool = True
    cloud_enabled: bool = False

    fallback_base_url: str | None = None
    fallback_model: str | None = None
    fallback_first_token_timeout_seconds: int = Field(default=0, ge=0, le=300)

    @field_validator("cloud_enabled", "anonymize_sensitive_data", mode="before")
    @classmethod
    def _coerce_bool(cls, v: object) -> object:
        if v == "":
            return False
        return v


class AgentSettings(BaseSettings):
    """Konfiguration für das Agent-System."""

    model_config = SettingsConfigDict(env_prefix="AGENT_", env_file=".env", extra="ignore")

    mode: AgentMode = AgentMode.ADVISORY
    max_iterations: int = Field(default=10, ge=1, le=100)
    approval_timeout_seconds: int = Field(default=3600, ge=60)
    dry_run: bool = True
    auto_backup: bool = True


class HASettings(BaseSettings):
    """Verbindungseinstellungen für Home Assistant."""

    model_config = SettingsConfigDict(env_prefix="HA_", env_file=".env", extra="ignore")

    url: str = "http://homeassistant.local:8123"
    token: SecretStr | None = None
    config_path: str = "/config"
    verify_ssl: bool = True
    websocket_timeout: int = 30


class SecuritySettings(BaseSettings):
    """Sicherheitseinstellungen des Agent-Systems selbst."""

    model_config = SettingsConfigDict(env_prefix="SECURITY_", env_file=".env", extra="ignore")

    allow_autonomous_file_writes: bool = False
    allow_shell_execution: bool = False
    allow_docker_access: bool = False
    max_file_size_bytes: int = Field(default=10_485_760, ge=1)


class _AppBaseSettings(BaseSettings):
    """Nur die flachen App-Felder — keine verschachtelten BaseSettings."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "HA Self-Healing Agent"
    version: str = "1.4.0"
    debug: bool = False
    host: str = "0.0.0.0"  # noqa: S104
    port: int = Field(default=8765, ge=1, le=65535)

    log_level: str = "INFO"
    log_format: str = "text"

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        upper = v.upper()
        if upper not in valid:
            raise ValueError(f"log_level muss einer von {valid} sein")
        return upper


@dataclass
class AppSettings:
    """
    Aggregierte App-Konfiguration.

    Jede Sub-Settings-Instanz liest eigenständig aus der Umgebung mit
    ihrem eigenen env_prefix — das ist das korrekte pydantic-settings v2 Muster
    für verschachtelte Konfigurationen mit unterschiedlichen Prefixen.
    """

    app_name: str = field(default="HA Self-Healing Agent")
    version: str = field(default="1.4.0")
    debug: bool = field(default=False)
    host: str = field(default="0.0.0.0")
    port: int = field(default=8765)
    log_level: str = field(default="INFO")
    log_format: str = field(default="text")

    llm: LLMSettings = field(default_factory=LLMSettings)
    agent: AgentSettings = field(default_factory=AgentSettings)
    ha: HASettings = field(default_factory=HASettings)
    security: SecuritySettings = field(default_factory=SecuritySettings)

    @classmethod
    def load(cls) -> "AppSettings":
        base = _AppBaseSettings()
        return cls(
            app_name=base.app_name,
            version=base.version,
            debug=base.debug,
            host=base.host,
            port=base.port,
            log_level=base.log_level,
            log_format=base.log_format,
            llm=LLMSettings(),
            agent=AgentSettings(),
            ha=HASettings(),
            security=SecuritySettings(),
        )


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Gibt die gecachte App-Konfiguration zurück (Singleton)."""
    return AppSettings.load()
