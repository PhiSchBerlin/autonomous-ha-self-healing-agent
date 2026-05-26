"""
Dashboard API-Client — zentraler HTTP-Layer für das Streamlit-Dashboard.

Kapselt alle Aufrufe an das Agent-Backend so dass die Seiten-Funktionen
in streamlit_app.py keine httpx-Imports oder URL-Konstruktion enthalten.
"""

from __future__ import annotations

import os
from typing import Any

import httpx

API_URL: str = os.getenv("AGENT_API_URL", "http://localhost:8765")
EXTERNAL_API_URL: str = os.getenv(
    "AGENT_EXTERNAL_API_URL",
    API_URL.replace("localhost", "192.168.178.48").replace("127.0.0.1", "192.168.178.48"),
)
REFRESH_SECONDS: int = int(os.getenv("DASHBOARD_REFRESH_SECONDS", "30"))


def get_json(path: str, timeout: float = 5.0) -> dict[str, Any] | list[Any] | None:
    """GET-Request — gibt None bei Fehler zurück (kein Exception-Raise)."""
    try:
        resp = httpx.get(f"{API_URL}{path}", timeout=timeout)
        resp.raise_for_status()
        return resp.json()  # type: ignore[return-value]
    except Exception:
        return None


def post_json(
    path: str,
    payload: dict[str, Any],
    timeout: float = 180.0,
) -> dict[str, Any] | None:
    """POST-Request — gibt None bei Fehler zurück."""
    try:
        resp = httpx.post(f"{API_URL}{path}", json=payload, timeout=timeout)
        resp.raise_for_status()
        return resp.json()  # type: ignore[return-value]
    except Exception:
        return None


def get_text(path: str, timeout: float = 5.0) -> str | None:
    """GET-Request für Plaintext (z.B. Prometheus-Metriken)."""
    try:
        resp = httpx.get(f"{API_URL}{path}", timeout=timeout)
        resp.raise_for_status()
        return resp.text
    except Exception:
        return None


def poll_repair_job(job_id: str) -> dict[str, Any] | None:
    """Pollt den Status eines Background-Repair-Jobs (kein Cache)."""
    return get_json(f"/api/v1/repair/jobs/{job_id}")  # type: ignore[return-value]


def fetch_health() -> dict[str, Any] | None:
    return get_json("/health")  # type: ignore[return-value]


def fetch_agent_status() -> dict[str, Any] | None:
    return get_json("/api/v1/agents/health")  # type: ignore[return-value]


def fetch_security_summary() -> dict[str, Any] | None:
    return get_json("/api/v1/security/issues/summary")  # type: ignore[return-value]


def fetch_security_issues(severity: str | None = None) -> list[dict[str, Any]]:
    params = f"?severity={severity}" if severity else ""
    result = get_json(f"/api/v1/security/issues{params}")
    return result if isinstance(result, list) else []


def fetch_pending_repairs() -> dict[str, Any] | None:
    return get_json("/api/v1/repair/pending")  # type: ignore[return-value]


def fetch_repair_history(max_commits: int = 50) -> dict[str, Any] | None:
    return get_json(f"/api/v1/repair/history?max_commits={max_commits}")  # type: ignore[return-value]


def fetch_knowledge_stats() -> dict[str, Any] | None:
    return get_json("/api/v1/knowledge/stats")  # type: ignore[return-value]


def fetch_latest_audit() -> dict[str, Any] | None:
    return get_json("/api/v1/agents/full-audit/latest")  # type: ignore[return-value]


def fetch_metrics() -> str | None:
    return get_text("/metrics")
