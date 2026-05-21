"""
Prometheus-Metriken für den HA Self-Healing Agent.

Exportiert Metriken über /metrics-Endpunkt (Prometheus-Format).
Fallback auf Stub-Objekte wenn prometheus_client nicht installiert ist.
"""

from __future__ import annotations

import logging
import time
from contextlib import contextmanager
from typing import Any, Generator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Stub-Klassen als Fallback wenn prometheus_client fehlt
# ---------------------------------------------------------------------------


class _StubMetric:
    def labels(self, **_: Any) -> "_StubMetric":
        return self

    def inc(self, _amount: float = 1) -> None:
        pass

    def dec(self, _amount: float = 1) -> None:
        pass

    def set(self, _value: float) -> None:
        pass

    def observe(self, _value: float) -> None:
        pass

    def time(self) -> Any:
        return self

    def __enter__(self) -> "_StubMetric":
        return self

    def __exit__(self, *_: Any) -> None:
        pass


def _try_import_prometheus() -> bool:
    """Gibt True zurück wenn prometheus_client verfügbar ist."""
    try:
        import prometheus_client  # noqa: F401
        return True
    except ImportError:
        return False


_PROMETHEUS_AVAILABLE = _try_import_prometheus()


def _make_counter(name: str, doc: str, labels: list[str]) -> Any:
    if _PROMETHEUS_AVAILABLE:
        from prometheus_client import Counter
        return Counter(name, doc, labels)
    return _StubMetric()


def _make_gauge(name: str, doc: str, labels: list[str] | None = None) -> Any:
    if _PROMETHEUS_AVAILABLE:
        from prometheus_client import Gauge
        return Gauge(name, doc, labels or [])
    return _StubMetric()


def _make_histogram(name: str, doc: str, labels: list[str], buckets: list[float] | None = None) -> Any:
    if _PROMETHEUS_AVAILABLE:
        from prometheus_client import Histogram
        kwargs: dict[str, Any] = {"name": name, "documentation": doc, "labelnames": labels}
        if buckets:
            kwargs["buckets"] = buckets
        return Histogram(**kwargs)
    return _StubMetric()


# ---------------------------------------------------------------------------
# Agent-Metriken
# ---------------------------------------------------------------------------

AGENT_RUNS_TOTAL = _make_counter(
    "ha_agent_runs_total",
    "Gesamtanzahl der Agent-Läufe",
    ["agent_type", "mode", "status"],
)

AGENT_RUN_DURATION_SECONDS = _make_histogram(
    "ha_agent_run_duration_seconds",
    "Dauer eines Agent-Laufs in Sekunden",
    ["agent_type", "mode"],
    buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0, 300.0],
)

AGENT_ACTIVE_RUNS = _make_gauge(
    "ha_agent_active_runs",
    "Aktuell aktive Agent-Läufe",
    ["agent_type"],
)

# ---------------------------------------------------------------------------
# Findings-Metriken
# ---------------------------------------------------------------------------

FINDINGS_DETECTED_TOTAL = _make_counter(
    "ha_findings_detected_total",
    "Erkannte Findings nach Schweregrad und Kategorie",
    ["severity", "category"],
)

FINDINGS_ACTIVE = _make_gauge(
    "ha_findings_active",
    "Aktuell aktive (nicht behobene) Findings",
    ["severity"],
)

# ---------------------------------------------------------------------------
# Reparatur-Metriken
# ---------------------------------------------------------------------------

REPAIRS_PROPOSED_TOTAL = _make_counter(
    "ha_repairs_proposed_total",
    "Vorgeschlagene Reparaturen nach Risiko",
    ["risk_level"],
)

REPAIRS_APPLIED_TOTAL = _make_counter(
    "ha_repairs_applied_total",
    "Angewendete Reparaturen nach Status",
    ["status", "risk_level"],
)

REPAIRS_PENDING_APPROVALS = _make_gauge(
    "ha_repairs_pending_approvals",
    "Ausstehende Genehmigungsanfragen",
)

REPAIR_APPROVAL_WAIT_SECONDS = _make_histogram(
    "ha_repair_approval_wait_seconds",
    "Wartezeit auf Genehmigung in Sekunden",
    ["risk_level"],
    buckets=[60.0, 300.0, 900.0, 1800.0, 3600.0, 7200.0],
)

# ---------------------------------------------------------------------------
# Security-Metriken
# ---------------------------------------------------------------------------

SECURITY_ISSUES_TOTAL = _make_counter(
    "ha_security_issues_total",
    "Erkannte Security-Issues nach Typ und Schweregrad",
    ["check_type", "severity"],
)

SECURITY_SCANS_TOTAL = _make_counter(
    "ha_security_scans_total",
    "Durchgeführte Security-Scans",
    ["status"],
)

# ---------------------------------------------------------------------------
# LLM-Metriken
# ---------------------------------------------------------------------------

LLM_CALLS_TOTAL = _make_counter(
    "ha_llm_calls_total",
    "LLM-API-Aufrufe nach Backend und Status",
    ["backend", "status"],
)

LLM_CALL_DURATION_SECONDS = _make_histogram(
    "ha_llm_call_duration_seconds",
    "Dauer von LLM-Aufrufen in Sekunden",
    ["backend"],
    buckets=[0.5, 1.0, 2.5, 5.0, 10.0, 30.0, 60.0, 120.0],
)

LLM_TOKENS_TOTAL = _make_counter(
    "ha_llm_tokens_total",
    "Verbrauchte LLM-Tokens",
    ["backend", "direction"],  # direction: input | output
)

# ---------------------------------------------------------------------------
# HTTP-Metriken (Middleware)
# ---------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = _make_counter(
    "ha_http_requests_total",
    "HTTP-Anfragen nach Methode, Pfad und Status-Code",
    ["method", "path", "status_code"],
)

HTTP_REQUEST_DURATION_SECONDS = _make_histogram(
    "ha_http_request_duration_seconds",
    "HTTP-Antwortzeiten in Sekunden",
    ["method", "path"],
    buckets=[0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0],
)

# ---------------------------------------------------------------------------
# Vector-Store-Metriken
# ---------------------------------------------------------------------------

VECTOR_STORE_ENTRIES = _make_gauge(
    "ha_vector_store_entries",
    "Einträge im Vector-Store nach Kollektion",
    ["collection"],
)

VECTOR_STORE_QUERIES_TOTAL = _make_counter(
    "ha_vector_store_queries_total",
    "Abfragen am Vector-Store nach Kollektion",
    ["collection", "status"],
)

# ---------------------------------------------------------------------------
# HA-Verbindungsmetriken
# ---------------------------------------------------------------------------

HA_WEBSOCKET_CONNECTED = _make_gauge(
    "ha_websocket_connected",
    "WebSocket-Verbindungsstatus zu Home Assistant (1=verbunden, 0=getrennt)",
)

HA_EVENTS_RECEIVED_TOTAL = _make_counter(
    "ha_events_received_total",
    "Von HA empfangene Events nach Typ",
    ["event_type"],
)

# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------


@contextmanager
def track_agent_run(agent_type: str, mode: str) -> Generator[None, None, None]:
    """Context Manager: trackt Laufzeit und Ergebnis eines Agent-Laufs."""
    AGENT_ACTIVE_RUNS.labels(agent_type=agent_type).inc()
    start = time.monotonic()
    status = "success"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        duration = time.monotonic() - start
        AGENT_ACTIVE_RUNS.labels(agent_type=agent_type).dec()
        AGENT_RUNS_TOTAL.labels(agent_type=agent_type, mode=mode, status=status).inc()
        AGENT_RUN_DURATION_SECONDS.labels(agent_type=agent_type, mode=mode).observe(duration)


def record_finding(severity: str, category: str) -> None:
    """Zählt ein erkanntes Finding."""
    FINDINGS_DETECTED_TOTAL.labels(severity=severity, category=category).inc()
    FINDINGS_ACTIVE.labels(severity=severity).inc()


def resolve_finding(severity: str) -> None:
    """Markiert ein Finding als behoben."""
    FINDINGS_ACTIVE.labels(severity=severity).dec()


def record_repair_proposed(risk_level: str) -> None:
    REPAIRS_PROPOSED_TOTAL.labels(risk_level=risk_level).inc()


def record_repair_applied(status: str, risk_level: str) -> None:
    REPAIRS_APPLIED_TOTAL.labels(status=status, risk_level=risk_level).inc()


def record_security_issue(check_type: str, severity: str) -> None:
    SECURITY_ISSUES_TOTAL.labels(check_type=check_type, severity=severity).inc()


def generate_metrics_output() -> tuple[bytes, str]:
    """
    Generiert Prometheus-Metrics-Output.

    Returns: (content_bytes, content_type)
    """
    if not _PROMETHEUS_AVAILABLE:
        return b"# prometheus_client not installed\n", "text/plain; charset=utf-8"

    from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
    return generate_latest(), CONTENT_TYPE_LATEST
