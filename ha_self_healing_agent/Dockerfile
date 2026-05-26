ARG BUILD_FROM=ghcr.io/home-assistant/amd64-base-python:3.12-alpine3.21
FROM ${BUILD_FROM}

RUN apt-get update && apt-get install -y --no-install-recommends \
    git curl gcc g++ make libffi-dev libssl-dev jq \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]" \
    "pydantic>=2.9.0" \
    "pydantic-settings>=2.6.0" \
    httpx \
    "langgraph>=0.2.50" \
    "langchain-core>=0.3.0" \
    anthropic \
    openai \
    structlog \
    gitpython \
    prometheus-client \
    "streamlit>=1.40.0" \
    "chromadb>=0.5.0"

ARG BUILD_VERSION=1.4.4
ARG CACHE_BUST=20260523-2
RUN echo "Cloning version ${BUILD_VERSION} (bust: ${CACHE_BUST})" && git clone --depth 1 --branch main \
    https://github.com/PhiSchBerlin/autonomous-ha-self-healing-agent.git /src \
    && cp -r /src/agent_core     ./agent_core \
    && cp -r /src/api            ./api \
    && cp -r /src/config         ./config \
    && cp -r /src/dashboard      ./dashboard \
    && cp -r /src/llm_backends   ./llm_backends \
    && cp -r /src/models         ./models \
    && cp -r /src/observability  ./observability \
    && cp -r /src/sandbox        ./sandbox \
    && cp -r /src/security       ./security \
    && cp    /src/main.py        ./main.py \
    && rm -rf /src

RUN mkdir -p /data/chromadb /data/backups /data/logs

# ONNX-Embedding-Modell beim Build cachen (79 MB) → kein Download beim ersten Start
ENV CHROMA_CACHE_DIR=/data/chroma_cache
RUN mkdir -p /data/chroma_cache && python - <<'EOF'
import os, sys
os.environ["CHROMA_CACHE_DIR"] = "/data/chroma_cache"
try:
    import chromadb
    from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
    ef = ONNXMiniLM_L6_V2(preferred_providers=["CPUExecutionProvider"])
    ef(["warmup"])
    print("ONNX model pre-cached to /data/chroma_cache")
except Exception as e:
    print(f"ONNX warmup fehlgeschlagen (nicht kritisch): {e}", file=sys.stderr)
EOF

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
