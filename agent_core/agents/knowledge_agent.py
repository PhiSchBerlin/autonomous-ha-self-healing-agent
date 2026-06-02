"""
Knowledge Agent — Persistentes Gedächtnis und RAG-System.

Verwaltet das Langzeitgedächtnis des HA Self-Healing Agents:
- Speichert Findings und Reparaturen nach jedem Workflow-Lauf
- Reichert neue Findings mit historischem Kontext an
- Pflegt eine HA-Wissensdatenbank (Best Practices, bekannte Bugs)
- Ermöglicht semantische Suche über vergangene Ereignisse

Workflow:
  ingest_findings → ingest_repairs → enrich_context → update_knowledge → done
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_core.memory.vector_store import HAVectorStore
from models.agent_models import AgentState
from models.enums import AgentStatus, AgentType

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

# Standard-Wissensbasis: bekannte HA-Probleme und Best Practices
_HA_KNOWLEDGE_BASE: list[dict[str, Any]] = [
    {
        "topic": "MQTT-Integration Best Practice",
        "content": (
            "MQTT-Broker-Zugangsdaten NIEMALS im Klartext in configuration.yaml speichern. "
            "Verwende !secret mqtt_password und secrets.yaml. "
            "broker: !secret mqtt_host, username: !secret mqtt_user, password: !secret mqtt_password"
        ),
        "tags": ["mqtt", "security", "secrets"],
    },
    {
        "topic": "HA Config-Check vor Neustart",
        "content": (
            "Vor jedem HA-Neustart: 'ha core check' oder Developer Tools → YAML-Check ausführen. "
            "Syntaxfehler in configuration.yaml führen zu kompletten Boot-Fehlern. "
            "Backup via Git-Tag empfohlen."
        ),
        "tags": ["config", "restart", "validation"],
    },
    {
        "topic": "Custom Component Deprecation Warning",
        "content": (
            "data_template: ist seit HA 2021.12 deprecated — verwende action: statt. "
            "entity_id in service calls: deprecated ab 2022.9 — verwende target: entity_id: statt. "
            "whitelist/blacklist deprecated — verwende allowlist/denylist."
        ),
        "tags": ["deprecation", "yaml", "automation"],
    },
    {
        "topic": "ESPHome Dallas/One-Wire Migration",
        "content": (
            "dallas: Platform wurde in ESPHome 2023.x zu one_wire: umbenannt. "
            "Migration: 'dallas:' → 'one_wire:' in ESPHome YAML. "
            "OTA-Konfiguration: 'ota:' → 'ota:\\n  - platform: esphome'"
        ),
        "tags": ["esphome", "migration", "dallas", "one_wire"],
    },
    {
        "topic": "Jinja2-Template-Sicherheit in HA",
        "content": (
            "HA Jinja2-Templates laufen in einer Sandbox — Zugriff auf __class__, __mro__, "
            "config-Objekt etc. deutet auf SSTI-Angriff hin. "
            "Benutzereingaben NIEMALS direkt in Templates einsetzen. "
            "shell_command mit Templates immer validieren."
        ),
        "tags": ["security", "jinja2", "ssti", "template"],
    },
    {
        "topic": "Zigbee2MQTT Integration Fehlerbehandlung",
        "content": (
            "Zigbee2MQTT-Fehler 'MQTT publish error' oft durch falschen Topic-Prefix. "
            "Standard base_topic: zigbee2mqtt. Prüfen: Permit Join deaktiviert? "
            "Device nicht in allowed_list? Coordinator-Firmware veraltet?"
        ),
        "tags": ["zigbee2mqtt", "mqtt", "error"],
    },
    {
        "topic": "HA Python Custom Component Best Practices",
        "content": (
            "Kein blocking I/O in async Funktionen (requests → httpx/aiohttp). "
            "hass.data[DOMAIN] deprecated → entry.runtime_data verwenden (HA 2024.x). "
            "ConfigEntry.async_on_unload() für Cleanup nutzen. "
            "Keine assert-Statements für Sicherheitsprüfungen."
        ),
        "tags": ["custom_component", "python", "async", "best_practice"],
    },
    {
        "topic": "Docker und HA Addon Sicherheit",
        "content": (
            "privileged: true in docker-compose ist ein kritisches Sicherheitsrisiko. "
            "Verwende capabilities statt privileged. "
            "Secrets nicht als ENV-Variablen sondern als Docker Secrets mounten. "
            "Images immer pinnen (kein :latest in Produktion)."
        ),
        "tags": ["docker", "security", "addon", "privilege"],
    },
]


class KnowledgeAgent(BaseAgent):
    """
    Persistentes Gedächtnis des HA Self-Healing Agent.

    Wird nach jedem Workflow-Lauf aufgerufen um:
    1. Neue Findings zu speichern
    2. Neue Reparaturen zu speichern
    3. Kontext für aktuelle Findings anzureichern
    4. Die HA-Wissensbasis aktuell zu halten
    """

    agent_type = AgentType.KNOWLEDGE

    def __init__(self, llm: Any, config: Any, persist_directory: str = "/data/chromadb") -> None:
        super().__init__(llm, config)
        self._vector_store = HAVectorStore(persist_directory=persist_directory)
        self._knowledge_seeded = False

    def _build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(AgentState)

        graph.add_node("seed_knowledge", self._node_seed_knowledge)
        graph.add_node("ingest_findings", self._node_ingest_findings)
        graph.add_node("ingest_repairs", self._node_ingest_repairs)
        graph.add_node("enrich_context", self._node_enrich_context)
        graph.add_node("done", self._node_done)

        graph.add_edge(START, "seed_knowledge")
        graph.add_edge("seed_knowledge", "ingest_findings")
        graph.add_edge("ingest_findings", "ingest_repairs")
        graph.add_edge("ingest_repairs", "enrich_context")
        graph.add_edge("enrich_context", "done")
        graph.add_edge("done", END)

        return graph.compile(checkpointer=MemorySaver())

    # -------------------------------------------------------------------------
    # Graph-Knoten
    # -------------------------------------------------------------------------

    async def _node_seed_knowledge(self, state: AgentState) -> dict[str, Any]:
        """Füllt die Wissensbasis beim ersten Start mit HA-Best-Practices."""
        if self._knowledge_seeded:
            return {"iteration": state.iteration}

        try:
            stats = self._vector_store.get_stats()
            if stats.get("ha_knowledge", 0) == 0:
                logger.info("Initialisiere HA-Wissensbasis mit %d Einträgen", len(_HA_KNOWLEDGE_BASE))
                for entry in _HA_KNOWLEDGE_BASE:
                    self._vector_store.store_knowledge(
                        topic=entry["topic"],
                        content=entry["content"],
                        source="builtin",
                        tags=entry.get("tags", []),
                    )
            self._knowledge_seeded = True
        except Exception as exc:
            logger.warning("Wissens-Seed fehlgeschlagen (ChromaDB nicht verfügbar?): %s", exc)

        return {"iteration": state.iteration}

    async def _node_ingest_findings(self, state: AgentState) -> dict[str, Any]:
        """Speichert alle Findings des aktuellen Runs im Vector-Store.

        Liest Findings aus state.findings (direkter Workflow) und zusätzlich
        aus context["ingest_findings"] (übergeben vom Orchestrator nach Full Audit).
        """
        from models.log_models import Finding

        # Findings aus State + aus Context zusammenführen
        all_findings: list[Any] = list(state.findings)
        for raw in state.context.get("ingest_findings", []):
            try:
                all_findings.append(Finding(**raw) if isinstance(raw, dict) else raw)
            except Exception:
                pass

        stored = 0
        for finding in all_findings:
            try:
                self._vector_store.store_finding(
                    title=finding.title,
                    description=finding.description,
                    severity=str(finding.severity),
                    category=str(finding.category),
                    source_agent=str(finding.source_agent),
                    finding_id=str(finding.id),
                    extra_metadata={
                        "risk_score": str(finding.risk_score),
                        "confidence": str(finding.confidence),
                        "run_id": str(state.run_id),
                    },
                )
                stored += 1
            except Exception as exc:
                logger.debug("Finding %s nicht gespeichert: %s", finding.id, exc)

        if stored:
            logger.info("Knowledge Agent: %d Findings gespeichert", stored)
        return {"iteration": state.iteration}

    async def _node_ingest_repairs(self, state: AgentState) -> dict[str, Any]:
        """Speichert alle RepairActions des aktuellen Runs.

        Liest aus state.repair_actions (direkter Workflow) und zusätzlich
        aus context["ingest_repairs"] (übergeben vom Orchestrator nach Full Audit).
        """
        from models.repair_models import RepairAction

        all_repairs: list[Any] = list(state.repair_actions)
        for raw in state.context.get("ingest_repairs", []):
            try:
                all_repairs.append(RepairAction(**raw) if isinstance(raw, dict) else raw)
            except Exception:
                pass

        stored = 0
        for repair in all_repairs:
            try:
                self._vector_store.store_repair(
                    title=repair.title,
                    rationale=repair.rationale,
                    finding_title=repair.title,
                    success=str(repair.status) in ("applied", "monitored"),
                    risk_level=str(repair.risk_level),
                    repair_id=str(repair.id),
                    file_paths=[c.file_path for c in repair.changes],
                )
                stored += 1
            except Exception as exc:
                logger.debug("Repair %s nicht gespeichert: %s", repair.id, exc)

        if stored:
            logger.info("Knowledge Agent: %d Reparaturen gespeichert", stored)
        return {"iteration": state.iteration}

    async def _node_enrich_context(self, state: AgentState) -> dict[str, Any]:
        """
        Reichert aktuelle Findings mit historischem Kontext an.

        Fügt relevante vergangene Reparaturen und HA-Wissen in den
        Agent-State ein damit nachfolgende Agenten davon profitieren.
        """
        enriched_context: list[dict[str, Any]] = []

        for finding in state.findings[:5]:
            try:
                context = self._vector_store.get_context_for_finding(
                    finding.title, finding.description
                )
                if context:
                    enriched_context.append({
                        "finding_id": str(finding.id),
                        "finding_title": finding.title,
                        "historical_context": context,
                    })
            except Exception as exc:
                logger.debug("Kontext-Anreicherung für %s fehlgeschlagen: %s", finding.id, exc)

        if enriched_context:
            logger.info(
                "Knowledge Agent: %d Findings mit historischem Kontext angereichert",
                len(enriched_context),
            )

        return {
            "context": {
                **state.context,
                "knowledge_context": enriched_context,
            }
        }

    async def _node_done(self, state: AgentState) -> dict[str, Any]:
        try:
            stats = self._vector_store.get_stats()
            logger.info(
                "Knowledge Agent abgeschlossen: %d Findings, %d Reparaturen, %d Wissenseinträge gespeichert",
                stats.get("ha_findings", 0),
                stats.get("ha_repairs", 0),
                stats.get("ha_knowledge", 0),
            )
        except Exception:
            pass
        return {"status": AgentStatus.IDLE}

    # -------------------------------------------------------------------------
    # Direkt aufrufbare Methoden (ohne Workflow)
    # -------------------------------------------------------------------------

    def search(self, query: str, n_results: int = 5) -> dict[str, Any]:
        """Führt eine semantische Suche über alle Kollektionen durch."""
        return {
            "findings": [
                {"text": e.text, "metadata": e.metadata, "distance": e.distance}
                for e in self._vector_store.search_similar_findings(query, n_results)
            ],
            "repairs": [
                {"text": e.text, "metadata": e.metadata, "distance": e.distance}
                for e in self._vector_store.search_similar_repairs(query, n_results)
            ],
            "knowledge": [
                {"text": e.text, "metadata": e.metadata, "distance": e.distance}
                for e in self._vector_store.search_knowledge(query, n_results)
            ],
        }

    def get_stats(self) -> dict[str, int]:
        """Gibt Statistiken über die gespeicherten Einträge zurück."""
        try:
            return self._vector_store.get_stats()
        except Exception:
            return {"total_entries": 0}

    def add_knowledge(
        self,
        topic: str,
        content: str,
        source: str = "user",
        tags: list[str] | None = None,
    ) -> str:
        """Fügt manuell einen Wissenseintrag hinzu."""
        return self._vector_store.store_knowledge(topic, content, source, tags)
