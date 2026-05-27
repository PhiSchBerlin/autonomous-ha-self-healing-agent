#!/usr/bin/with-contenv bashio
# shellcheck shell=bash
set -euo pipefail

bashio::log.info "HA Self-Healing Agent startet..."

export AGENT_MODE=$(bashio::config 'agent_mode')
export LLM_BACKEND_TYPE=$(bashio::config 'llm_backend_type')
export LLM_MODEL=$(bashio::config 'llm_model')
export LLM_BASE_URL=$(bashio::config 'llm_base_url')
export LLM_CLOUD_ENABLED=$(bashio::config 'llm_cloud_enabled')
export LLM_ANONYMIZE_SENSITIVE_DATA=$(bashio::config 'llm_anonymize_sensitive_data')
export LLM_API_KEY=$(bashio::config 'llm_api_key' '')
export LOG_LEVEL=$(bashio::config 'log_level')
export AGENT_APPROVAL_TIMEOUT_SECONDS=$(bashio::config 'approval_timeout_seconds')
export SECURITY_ALLOW_AUTONOMOUS_FILE_WRITES=$(bashio::config 'allow_autonomous_file_writes')
export DASHBOARD_REFRESH_SECONDS=$(bashio::config 'dashboard_refresh_seconds')
export LLM_FALLBACK_BASE_URL=$(bashio::config 'llm_fallback_base_url' '')
export LLM_FALLBACK_MODEL=$(bashio::config 'llm_fallback_model' '')
export LLM_FALLBACK_FIRST_TOKEN_TIMEOUT_SECONDS=$(bashio::config 'llm_fallback_first_token_timeout_seconds')

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
export PYTHONPATH="/app"

bashio::log.info "Modus: ${AGENT_MODE} | LLM: ${LLM_BACKEND_TYPE}/${LLM_MODEL}"

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
bashio::log.info "Streamlit gestartet (PID: ${STREAMLIT_PID})"

# FastAPI Agent im Vordergrund starten
cd /app
exec python main.py
