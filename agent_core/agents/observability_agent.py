"""
Observability Agent — Post-Apply-Monitoring und HA-Zustandsverfolgung.

Aufgaben:
- Überwacht HA-Zustand nach einer Reparatur (Post-Apply-Health-Check)
- Verfolgt HA-Entity-Zustandsänderungen über WebSocket-Events
- Exportiert Metriken (Findings-Rate, Repair-Erfolg, HA-Availability)
- Erkennt Regressionen: neue Fehler nach einer Reparatur

Workflow:
  collect_ha_state → evaluate_health → check_regressions → update_metrics → done
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from models.agent_models import AgentState
from models.enums import AgentStatus, AgentType

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)


class ObservabilityAgent(BaseAgent):
    """
    Monitoring-Agent für Post-Apply-Validierung und Zustandsüberwachung.

    Nach jeder Reparatur prüft dieser Agent:
    1. Ist HA noch erreichbar?
    2. Sind bekannte Services noch aktiv?
    3. Gibt es neue Fehler in den Logs (Regressionen)?
    4. Wie entwickeln sich Findings und Repairs über Zeit?
    """

    agent_type = AgentType.OBSERVABILITY

    def __init__(self, llm: Any, config: Any, ha_client: Any | None = None, **kwargs: Any) -> None:
        super().__init__(llm, config, **kwargs)
        self._ha_client = ha_client
        self._baseline: dict[str, Any] = {}

    def set_baseline(self, ha_state: dict[str, Any]) -> None:
        """Speichert den HA-Zustand vor einer Reparatur als Baseline."""
        self._baseline = dict(ha_state)
        logger.info("Baseline für %d Entities gesetzt", len(ha_state))

    def _build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(AgentState)

        graph.add_node("collect_ha_state", self._node_collect_ha_state)
        graph.add_node("evaluate_health", self._node_evaluate_health)
        graph.add_node("check_regressions", self._node_check_regressions)
        graph.add_node("update_metrics", self._node_update_metrics)
        graph.add_node("done", self._node_done)

        graph.add_edge(START, "collect_ha_state")
        graph.add_edge("collect_ha_state", "evaluate_health")
        graph.add_edge("evaluate_health", "check_regressions")
        graph.add_edge("check_regressions", "update_metrics")
        graph.add_edge("update_metrics", "done")
        graph.add_edge("done", END)

        return graph.compile()

    # -------------------------------------------------------------------------
    # Graph-Knoten
    # -------------------------------------------------------------------------

    async def _node_collect_ha_state(self, state: AgentState) -> AgentState:
        """Sammelt den aktuellen HA-Zustand über den WebSocket-Client."""
        context = state.context

        if self._ha_client is not None:
            try:
                ha_state = await self._ha_client.get_all_states()
                context["current_ha_state"] = {
                    entity["entity_id"]: entity.get("state")
                    for entity in ha_state
                    if entity.get("entity_id")
                }
                context["ha_reachable"] = True
                logger.debug("HA-Zustand gesammelt: %d Entities", len(context["current_ha_state"]))
            except Exception as exc:
                logger.warning("HA nicht erreichbar: %s", exc)
                context["ha_reachable"] = False
                context["current_ha_state"] = {}
        else:
            # Kein Client vorhanden — Daten aus context übernehmen
            context.setdefault("ha_reachable", context.get("ha_reachable", True))
            context.setdefault("current_ha_state", context.get("current_ha_state", {}))

        context["collected_at"] = datetime.now(UTC).isoformat()
        state.context = context
        return state

    async def _node_evaluate_health(self, state: AgentState) -> AgentState:
        """Bewertet den allgemeinen HA-Gesundheitszustand."""
        context = state.context
        ha_reachable = context.get("ha_reachable", True)
        current_state = context.get("current_ha_state", {})

        health_score = 1.0 if ha_reachable else 0.0

        # Zähle unavailable/unknown Entities
        unavailable = [
            eid for eid, s in current_state.items()
            if s in ("unavailable", "unknown")
        ]
        if current_state:
            unavailable_ratio = len(unavailable) / len(current_state)
            # Gesundheit reduziert sich proportional zu unavailable Entities
            health_score = max(0.0, health_score - unavailable_ratio * 0.5)

        context["health_score"] = round(health_score, 3)
        context["unavailable_entities"] = unavailable
        context["unavailable_count"] = len(unavailable)

        logger.info(
            "Health-Score: %.3f (unavailable: %d/%d)",
            health_score, len(unavailable), len(current_state),
        )
        state.context = context
        return state

    async def _node_check_regressions(self, state: AgentState) -> AgentState:
        """Vergleicht aktuellen Zustand mit Baseline zur Regressionserkennung."""
        context = state.context
        current_state = context.get("current_ha_state", {})
        regressions: list[dict[str, Any]] = []

        if self._baseline:
            for entity_id, baseline_state in self._baseline.items():
                current = current_state.get(entity_id)
                if current is None:
                    regressions.append({
                        "entity_id": entity_id,
                        "type": "disappeared",
                        "baseline": baseline_state,
                        "current": None,
                    })
                elif baseline_state not in ("unavailable", "unknown") and current in ("unavailable", "unknown"):
                    regressions.append({
                        "entity_id": entity_id,
                        "type": "degraded",
                        "baseline": baseline_state,
                        "current": current,
                    })

        context["regressions"] = regressions
        context["regression_count"] = len(regressions)
        context["has_regressions"] = bool(regressions)

        if regressions:
            logger.warning("Regressionen erkannt: %d Entities betroffen", len(regressions))
            for reg in regressions[:5]:
                logger.warning("  %s: %s → %s", reg["entity_id"], reg["baseline"], reg["current"])
        else:
            logger.info("Keine Regressionen erkannt")

        state.context = context
        return state

    async def _node_update_metrics(self, state: AgentState) -> AgentState:
        """Aktualisiert Prometheus-Metriken basierend auf dem aktuellen Zustand."""
        from observability.metrics import (
            FINDINGS_ACTIVE,
            HA_WEBSOCKET_CONNECTED,
            VECTOR_STORE_ENTRIES,
        )

        context = state.context
        ha_reachable = context.get("ha_reachable", True)

        # HA-Verbindungsstatus
        HA_WEBSOCKET_CONNECTED.set(1.0 if ha_reachable else 0.0)

        # Aktive Findings aus context
        for severity in ("critical", "high", "medium", "low", "info"):
            count = context.get(f"active_findings_{severity}", 0)
            FINDINGS_ACTIVE.labels(severity=severity).set(count)

        # Vector-Store-Statistiken falls vorhanden
        vs_stats = context.get("vector_store_stats", {})
        for collection, count in vs_stats.items():
            if collection != "total_entries":
                VECTOR_STORE_ENTRIES.labels(collection=collection).set(count)

        state.context = context
        return state

    async def _node_done(self, state: AgentState) -> AgentState:
        context = state.context
        regression_count = context.get("regression_count", 0)
        health_score = context.get("health_score", 1.0)

        if regression_count > 0 or health_score < 0.5:
            state.status = AgentStatus.ERROR
            state.errors.append(
                f"Post-Apply-Check: {regression_count} Regressionen, "
                f"Health-Score {health_score:.3f}"
            )
        else:
            state.status = AgentStatus.IDLE

        return state

    # -------------------------------------------------------------------------
    # Direkte API-Methoden (ohne Workflow)
    # -------------------------------------------------------------------------

    def get_health_summary(self) -> dict[str, Any]:
        """Gibt eine kompakte Zusammenfassung des letzten Monitoring-Laufs zurück."""
        return {
            "baseline_entities": len(self._baseline),
            "agent_type": self.agent_type,
        }
