"""FastAPI-Anwendung für den HA Self-Healing Agent."""

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from api.middleware.metrics_middleware import PrometheusMetricsMiddleware
from config.settings import get_settings
from observability.logging import get_logger, setup_logging

logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup- und Shutdown-Lifecycle der FastAPI-App."""
    settings = get_settings()
    setup_logging(level=settings.log_level, log_format=settings.log_format)
    logger.info("HA Self-Healing Agent startet (version=%s, mode=%s)", settings.version, settings.agent.mode)

    # ChromaDB + Embedding-Modell vorab initialisieren damit der erste API-Aufruf
    # nicht durch den ONNX-Download blockiert wird (Modell wird in /data/chroma_cache
    # gecacht und muss nur beim allerersten Start heruntergeladen werden).
    try:
        from agent_core.memory.vector_store import HAVectorStore
        await asyncio.to_thread(HAVectorStore().get_stats)
        logger.info("ChromaDB Warm-up abgeschlossen")
    except Exception as _exc:
        logger.warning("ChromaDB Warm-up fehlgeschlagen (nicht kritisch): %s", _exc)

    yield

    logger.info("HA Self-Healing Agent wird heruntergefahren")


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        version=settings.version,
        description=(
            "Autonomer Home Assistant Self-Healing & Security AI Agent. "
            "Analysiert Logs, erkennt Fehler und schlägt Reparaturen vor."
        ),
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost", "http://homeassistant.local"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.add_middleware(PrometheusMetricsMiddleware)

    from api.routers.agent import router as agent_router
    from api.routers.knowledge import router as knowledge_router
    from api.routers.observability import router as observability_router
    from api.routers.repair import router as repair_router
    from api.routers.security import router as security_router

    # Router einzeln einbinden damit ein Fehler in einem Router die anderen nicht blockiert
    for _router, _prefix in (
        (agent_router, "/api/v1"),
        (security_router, "/api/v1"),
        (repair_router, "/api/v1"),
        (knowledge_router, "/api/v1"),
        (observability_router, ""),
    ):
        try:
            app.include_router(_router, prefix=_prefix) if _prefix else app.include_router(_router)
        except Exception as _exc:
            import logging as _logging
            _logging.getLogger(__name__).error("Router-Registrierung fehlgeschlagen: %s", _exc)

    @app.get("/health", tags=["system"])
    async def health_check() -> dict[str, str]:
        return {"status": "ok", "version": settings.version}

    return app


app = create_app()
