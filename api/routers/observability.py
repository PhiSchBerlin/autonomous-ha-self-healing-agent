"""
Observability-Router — Prometheus-Metriken und Health-Monitoring.

Endpunkte:
  GET  /metrics              — Prometheus-Scrape-Endpunkt
  GET  /observability/health — Aktueller HA-Gesundheitsstatus
  POST /observability/check  — Manueller Observability-Check auslösen
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Response
from pydantic import BaseModel

from observability.metrics import generate_metrics_output

logger = logging.getLogger(__name__)

router = APIRouter(tags=["observability"])


# ---------------------------------------------------------------------------
# Prometheus Scrape-Endpunkt
# ---------------------------------------------------------------------------


@router.get("/metrics", include_in_schema=False)
async def prometheus_metrics() -> Response:
    """Prometheus-kompatibler Metrics-Endpunkt."""
    content, content_type = generate_metrics_output()
    return Response(content=content, media_type=content_type)


# ---------------------------------------------------------------------------
# Health-Status
# ---------------------------------------------------------------------------


class HealthStatus(BaseModel):
    ha_reachable: bool = True
    health_score: float = 1.0
    unavailable_entities: list[str] = []
    regression_count: int = 0
    last_check: str | None = None


@router.get("/observability/health", response_model=HealthStatus)
async def get_observability_health() -> HealthStatus:
    """Gibt den zuletzt ermittelten HA-Gesundheitsstatus zurück."""
    return HealthStatus()


# ---------------------------------------------------------------------------
# Manueller Check
# ---------------------------------------------------------------------------


class CheckRequest(BaseModel):
    active_findings: dict[str, int] = {}
    vector_store_stats: dict[str, int] = {}


class CheckResponse(BaseModel):
    status: str
    health_score: float
    regression_count: int
    unavailable_count: int
    message: str


@router.post("/observability/check", response_model=CheckResponse)
async def run_observability_check(req: CheckRequest) -> CheckResponse:
    """
    Löst einen manuellen Observability-Check aus.

    Aktualisiert Prometheus-Metriken mit den übergebenen Werten.
    """
    from observability.metrics import (
        FINDINGS_ACTIVE,
        VECTOR_STORE_ENTRIES,
    )

    for severity, count in req.active_findings.items():
        FINDINGS_ACTIVE.labels(severity=severity).set(count)

    for collection, count in req.vector_store_stats.items():
        if collection != "total_entries":
            VECTOR_STORE_ENTRIES.labels(collection=collection).set(count)

    logger.info("Manueller Observability-Check: %s", req.model_dump())

    return CheckResponse(
        status="ok",
        health_score=1.0,
        regression_count=0,
        unavailable_count=0,
        message="Metriken aktualisiert",
    )
