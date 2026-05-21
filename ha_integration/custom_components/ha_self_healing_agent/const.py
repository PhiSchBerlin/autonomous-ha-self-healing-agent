"""Konstanten für die HA Self-Healing Agent Integration."""

DOMAIN = "ha_self_healing_agent"

CONF_AGENT_URL = "agent_url"
CONF_AGENT_MODE = "agent_mode"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_AGENT_URL = "http://localhost:8765"
DEFAULT_SCAN_INTERVAL = 300  # Sekunden

AGENT_MODES = ["advisory", "approval", "autonomous"]

# Actions (ab HA 2024.8 intern als Actions behandelt)
ACTION_RUN_LOG_ANALYSIS = "run_log_analysis"
ACTION_RUN_SECURITY_SCAN = "run_security_scan"
ACTION_APPROVE_REPAIR = "approve_repair"
ACTION_REJECT_REPAIR = "reject_repair"
ACTION_RUN_FULL_AUDIT = "run_full_audit"

# Events (HA-interne Bus-Events)
EVENT_FINDING_DETECTED = f"{DOMAIN}_finding_detected"
EVENT_REPAIR_PROPOSED = f"{DOMAIN}_repair_proposed"
EVENT_REPAIR_APPLIED = f"{DOMAIN}_repair_applied"
EVENT_SECURITY_ALERT = f"{DOMAIN}_security_alert"
