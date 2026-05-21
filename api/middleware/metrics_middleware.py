"""
Prometheus-Metrics-Middleware für FastAPI.

Misst HTTP-Anfragen und Antwortzeiten automatisch für alle Routen.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Awaitable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match

from observability.metrics import HTTP_REQUEST_DURATION_SECONDS, HTTP_REQUESTS_TOTAL


def _get_route_path(request: Request) -> str:
    """
    Gibt den parametrisierten Pfad zurück (z.B. /api/v1/repair/{repair_id})
    statt des konkreten Pfads — verhindert hohe Kardinalität in Prometheus.
    """
    for route in request.app.routes:
        match, _ = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", request.url.path)
    return request.url.path


class PrometheusMetricsMiddleware(BaseHTTPMiddleware):
    """Middleware die HTTP-Metriken für jeden Request aufzeichnet."""

    EXCLUDED_PATHS = frozenset({"/metrics", "/health", "/docs", "/redoc", "/openapi.json"})

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path in self.EXCLUDED_PATHS:
            return await call_next(request)

        method = request.method
        path = _get_route_path(request)
        start = time.monotonic()

        response = await call_next(request)

        duration = time.monotonic() - start
        status_code = str(response.status_code)

        HTTP_REQUESTS_TOTAL.labels(method=method, path=path, status_code=status_code).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(method=method, path=path).observe(duration)

        return response
