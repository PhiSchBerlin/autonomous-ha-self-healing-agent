#!/bin/bash
set -euo pipefail

OPTIONS="/data/options.json"

jq_str() {
    jq -r --arg key "$1" '.[$key] // ""' "${OPTIONS}" 2>/dev/null || true
}

jq_bool() {
    jq -r --arg key "$1" 'if .[$key] == true then "true" else "false" end' "${OPTIONS}" 2>/dev/null || echo "false"
}

jq_int() {
    jq -r --arg key "$1" '.[$key] | numbers | tostring' "${OPTIONS}" 2>/dev/null || echo "$2"
}

export AGENT_MODE=$(jq_str 'agent_mode')
export LLM_BACKEND_TYPE=$(jq_str 'llm_backend_type')
export LLM_MODEL=$(jq_str 'llm_model')
export LLM_BASE_URL=$(jq_str 'llm_base_url')
export LLM_CLOUD_ENABLED=$(jq_bool 'llm_cloud_enabled')
export LLM_ANONYMIZE_SENSITIVE_DATA=$(jq_bool 'llm_anonymize_sensitive_data')
export LLM_API_KEY=$(jq_str 'llm_api_key')
export LOG_LEVEL=$(jq_str 'log_level')
export AGENT_APPROVAL_TIMEOUT_SECONDS=$(jq_int 'approval_timeout_seconds' '3600')
export SECURITY_ALLOW_AUTONOMOUS_FILE_WRITES=$(jq_bool 'allow_autonomous_file_writes')
export DASHBOARD_REFRESH_SECONDS=$(jq_int 'dashboard_refresh_seconds' '30')

export LLM_FALLBACK_BASE_URL=$(jq_str 'llm_fallback_base_url')
export LLM_FALLBACK_MODEL=$(jq_str 'llm_fallback_model')

export SECURITY_ALLOW_SHELL_EXECUTION="false"
export SECURITY_ALLOW_DOCKER_ACCESS="false"
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN:-}"
export HA_CONFIG_PATH="/config"
export AGENT_API_URL="http://localhost:8765"
export HOST="0.0.0.0"
export PORT="8765"
export LOG_FORMAT="json"
export PYTHONUNBUFFERED="1"

echo "HA Self-Healing Agent startet..."
echo "Modus: ${AGENT_MODE} | LLM: ${LLM_BACKEND_TYPE}/${LLM_MODEL}"

# Streamlit Dashboard im Hintergrund starten
streamlit run /app/dashboard/streamlit_app.py \
    --server.port 8501 \
    --server.address 0.0.0.0 \
    --server.headless true \
    --server.enableCORS false \
    --server.enableXsrfProtection false \
    --logger.level warning \
    &

STREAMLIT_PID=$!
echo "Streamlit gestartet (PID: ${STREAMLIT_PID})"

# FastAPI Agent im Vordergrund starten
cd /app
exec python main.py
