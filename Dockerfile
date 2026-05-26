# syntax=docker/dockerfile:1.7
# ============================================================
# Stage 1: Builder — Abhängigkeiten installieren
# ============================================================
FROM python:3.12-slim AS builder

WORKDIR /build

# Systemabhängigkeiten nur für den Build-Prozess
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# Nur pyproject.toml zuerst kopieren → besseres Layer-Caching
COPY pyproject.toml .

# Abhängigkeiten in virtualenv installieren
RUN python -m venv /opt/venv && \
    /opt/venv/bin/pip install --no-cache-dir --upgrade pip && \
    /opt/venv/bin/pip install --no-cache-dir -e .

# ============================================================
# Stage 2: Runtime — schlankes Produktions-Image
# ============================================================
FROM python:3.12-slim AS runtime

LABEL org.opencontainers.image.title="HA Self-Healing Agent" \
      org.opencontainers.image.description="Autonomer Home Assistant Self-Healing & Security AI Agent" \
      org.opencontainers.image.version="1.5.0" \
      org.opencontainers.image.licenses="MIT"

WORKDIR /app

# Laufzeit-Systemabhängigkeiten (curl für Healthcheck, git für GitOps)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        git \
    && rm -rf /var/lib/apt/lists/*

# Virtualenv aus Builder-Stage übernehmen
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Anwendungscode kopieren (ohne sensible Dateien via .dockerignore)
COPY . .

# Datenpersistenz-Verzeichnisse anlegen
RUN mkdir -p /data/chromadb /data/backups /data/logs

# Nicht-root User für Containersicherheit
RUN useradd -m -u 1000 -s /bin/sh agent && \
    chown -R agent:agent /app /data

USER agent

# Umgebungsvariablen mit sicheren Standardwerten
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    AGENT_MODE=advisory \
    HOST=0.0.0.0 \
    PORT=8765

EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT}/health || exit 1

CMD ["python", "main.py"]

# ============================================================
# Stage 3: Dashboard — Streamlit (optionaler separater Build)
# ============================================================
FROM runtime AS dashboard

EXPOSE 8501

CMD ["streamlit", "run", "dashboard/streamlit_app.py", \
     "--server.address=0.0.0.0", \
     "--server.port=8501", \
     "--server.headless=true", \
     "--browser.gatherUsageStats=false"]
