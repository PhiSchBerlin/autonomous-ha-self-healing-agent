"""Zentrale Enumerationen für das HA Self-Healing Agent System."""

from enum import StrEnum


class AgentMode(StrEnum):
    ADVISORY = "advisory"
    APPROVAL = "approval"
    AUTONOMOUS = "autonomous"


class AgentStatus(StrEnum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    ERROR = "error"
    PAUSED = "paused"


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingCategory(StrEnum):
    LOG_ERROR = "log_error"
    CONFIG_ERROR = "config_error"
    SECURITY = "security"
    PERFORMANCE = "performance"
    DEPRECATION = "deprecation"
    REGRESSION = "regression"


class RepairStatus(StrEnum):
    PROPOSED = "proposed"
    SIMULATED = "simulated"
    VALIDATED = "validated"
    APPROVED = "approved"
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    REJECTED = "rejected"
    FAILED = "failed"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class LogSource(StrEnum):
    HA_CORE = "ha_core"
    SUPERVISOR = "supervisor"
    ADDON = "addon"
    ESPHOME = "esphome"
    ZIGBEE2MQTT = "zigbee2mqtt"
    ZWAVE_JS = "zwave_js"
    MQTT = "mqtt"
    DOCKER = "docker"
    SYSTEMD = "systemd"
    REVERSE_PROXY = "reverse_proxy"
    FRONTEND = "frontend"


class SecurityCheckType(StrEnum):
    SECRETS_DETECTION = "secrets_detection"
    HARDCODED_CREDENTIALS = "hardcoded_credentials"
    INSECURE_TOKEN = "insecure_token"
    OPEN_PORT = "open_port"
    MISSING_AUTH = "missing_auth"
    PRIVILEGE_ESCALATION = "privilege_escalation"
    YAML_INJECTION = "yaml_injection"
    JINJA2_INJECTION = "jinja2_injection"
    SHELL_INJECTION = "shell_injection"
    PYTHON_EVAL = "python_eval"
    INSECURE_COMPONENT = "insecure_component"
    SUPPLY_CHAIN = "supply_chain"
    DEPENDENCY_VULNERABILITY = "dependency_vulnerability"
    DOCKER_SECURITY = "docker_security"
    MQTT_SECURITY = "mqtt_security"
    TLS_CONFIG = "tls_config"
    HTTP_SECURITY_HEADERS = "http_security_headers"
    REVERSE_PROXY_SECURITY = "reverse_proxy_security"
    CVE = "cve"


class LLMBackendType(StrEnum):
    OLLAMA = "ollama"
    LLAMA_CPP = "llama_cpp"
    VLLM = "vllm"
    LM_STUDIO = "lm_studio"
    LOCAL_AI = "local_ai"
    TABBY_API = "tabby_api"
    ANTHROPIC = "anthropic"
    OPENAI = "openai"
    GEMINI = "gemini"
    OPENROUTER = "openrouter"


class AgentType(StrEnum):
    LOG_ANALYSIS = "log_analysis"
    CONFIG_ANALYSIS = "config_analysis"
    SECURITY = "security"
    REPAIR = "repair"
    VALIDATION = "validation"
    OBSERVABILITY = "observability"
    KNOWLEDGE = "knowledge"
