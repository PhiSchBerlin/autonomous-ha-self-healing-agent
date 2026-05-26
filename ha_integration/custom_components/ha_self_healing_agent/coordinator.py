"""DataUpdateCoordinator für den HA Self-Healing Agent."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_AGENT_URL, CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN

_LOGGER = logging.getLogger(__name__)


class AgentCoordinatorData:
    """Strukturierte Daten vom Agent-Backend."""

    def __init__(self) -> None:
        self.agent_reachable: bool = False
        self.agent_mode: str = "advisory"
        self.agent_version: str = "unknown"
        self.findings_count: int = 0
        self.critical_findings: int = 0
        self.high_findings: int = 0
        self.security_issues_count: int = 0
        self.overall_risk_score: float = 0.0
        self.pending_approvals: int = 0
        self.last_run_id: str = ""
        self.last_scan_duration: float = 0.0
        self.repairs_applied: int = 0
        self.repairs_proposed: int = 0
        self.knowledge_entries: int = 0


class HAAgentCoordinator(DataUpdateCoordinator[AgentCoordinatorData]):
    """
    Koordiniert das regelmäßige Polling des Agent-Backends.

    Fragt Health, Status und Ergebnisse ab und stellt sie
    als HA-Entitäten zur Verfügung.
    """

    def __init__(
        self,
        hass: HomeAssistant,
        agent_url: str,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.agent_url = agent_url.rstrip("/")
        self._scan_interval = scan_interval

    async def _async_update_data(self) -> AgentCoordinatorData:
        """Fragt alle relevanten Daten vom Agent-Backend ab."""
        import httpx

        data = AgentCoordinatorData()

        try:
            async with httpx.AsyncClient(
                base_url=self.agent_url, timeout=15
            ) as client:
                # Health-Check
                health_resp = await client.get("/health")
                if health_resp.status_code == 200:
                    health = health_resp.json()
                    data.agent_reachable = True
                    data.agent_version = health.get("version", "unknown")
                else:
                    data.agent_reachable = False
                    return data

                # Agent-Status (pending approvals, Modus)
                try:
                    status_resp = await client.get("/api/v1/agents/health")
                    if status_resp.status_code == 200:
                        status = status_resp.json()
                        data.agent_mode = status.get("mode", "advisory")
                        data.pending_approvals = status.get("pending_approvals", 0)
                except Exception:
                    pass

                # Security-Issues-Zusammenfassung
                try:
                    sec_resp = await client.get("/api/v1/security/issues/summary")
                    if sec_resp.status_code == 200:
                        sec = sec_resp.json()
                        data.security_issues_count = sec.get("total", 0)
                except Exception:
                    pass

                # Audit-Ergebnisse (Findings, Risiko-Score)
                try:
                    audit_resp = await client.get("/api/v1/agents/full-audit/latest")
                    if audit_resp.status_code == 200:
                        audit = audit_resp.json()
                        data.findings_count = audit.get("findings_count", 0)
                        data.critical_findings = audit.get("critical_findings", 0)
                        data.high_findings = audit.get("high_findings", 0)
                        data.overall_risk_score = audit.get("overall_risk_score", 0.0)
                        data.last_run_id = audit.get("run_id", "")
                        data.last_scan_duration = audit.get("duration_seconds", 0.0)
                except Exception:
                    pass

                # Ausstehende Repair-Actions (proposed/validated)
                try:
                    pending_resp = await client.get("/api/v1/repair/pending")
                    if pending_resp.status_code == 200:
                        pending = pending_resp.json()
                        data.repairs_proposed = len(pending.get("pending", []))
                except Exception:
                    pass

                # Repair-History (angewendete Reparaturen)
                try:
                    history_resp = await client.get(
                        "/api/v1/repair/history", params={"max_commits": 50}
                    )
                    if history_resp.status_code == 200:
                        history = history_resp.json()
                        commits = history.get("commits", [])
                        data.repairs_applied = len(
                            [c for c in commits if c.get("message", "").startswith("fix:")]
                        )
                except Exception:
                    pass

                # Knowledge-Stats
                try:
                    knowledge_resp = await client.get("/api/v1/knowledge/stats")
                    if knowledge_resp.status_code == 200:
                        kstats = knowledge_resp.json()
                        data.knowledge_entries = kstats.get("total_entries", 0)
                except Exception:
                    pass

        except Exception as exc:
            _LOGGER.debug("Agent nicht erreichbar: %s", exc)
            data.agent_reachable = False

        return data
