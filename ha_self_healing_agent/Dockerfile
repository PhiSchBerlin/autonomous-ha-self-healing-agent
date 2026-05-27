FROM ghcr.io/home-assistant/aarch64-base-python:3.12-alpine3.21

ARG BUILD_VERSION=1.5.0
ARG BUILD_ARCH=aarch64
ARG CACHE_BUST=20260527-2

RUN apk add --no-cache \
    git curl gcc g++ make libffi-dev openssl-dev musl-dev jq

WORKDIR /app

RUN pip install --no-cache-dir --upgrade pip setuptools wheel

RUN pip install --no-cache-dir \
    "fastapi==0.115.5" \
    "uvicorn[standard]==0.32.1" \
    "pydantic==2.9.2" \
    "pydantic-settings==2.6.1" \
    "httpx==0.27.2" \
    "langgraph==0.2.50" \
    "langchain-core==0.3.25" \
    "anthropic==0.39.0" \
    "openai==1.54.5" \
    "structlog==24.4.0" \
    "gitpython==3.1.43" \
    "prometheus-client==0.21.0" \
    "streamlit==1.40.2" \
    "chromadb==0.6.3"

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

COPY run.sh /run.sh
RUN chmod a+x /run.sh

CMD ["/run.sh"]
