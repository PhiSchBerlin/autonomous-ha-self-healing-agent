FROM python:3.12-slim

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

ARG BUILD_VERSION=1.0.0
ARG CACHE_BUST=20260520-11
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

# ChromaDB ONNX-Embedding-Modell beim Build herunterladen (79 MB),
# damit der erste /knowledge/search-Aufruf nicht timeoutet.
RUN python - <<'EOF'
import chromadb
client = chromadb.PersistentClient(path="/data/chromadb")
col = client.get_or_create_collection("warmup")
col.add(documents=["warmup"], ids=["warmup-0"])
client.delete_collection("warmup")
print("ChromaDB ONNX model pre-downloaded.")
EOF

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
