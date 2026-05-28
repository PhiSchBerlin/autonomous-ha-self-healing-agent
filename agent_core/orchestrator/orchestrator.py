"""
Zentraler Agent-Orchestrator.

Koordiniert alle Agenten, verwaltet Approval-Requests und implementiert
den 8-stufigen Reparaturprozess (Analyse → Hypothese → Simulation →
Validation → Backup → Anwendung → Monitoring → Rollback).
"""

import asyncio
import logging
from typing import Any

from llm_backends.base import BaseLLMBackend
from models.agent_models import AgentRunConfig, WorkflowResult
from models.enums import AgentMode, AgentType

logger = logging.getLogger(__name__)


class AgentOrchestrator:
    """
    Koordiniert alle Agenten und steuert den übergeordneten Workflow.

    Im ADVISORY-Modus werden nur Empfehlungen ausgegeben.
    Im APPROVAL-Modus wartet das System auf menschliche Bestätigung.
    Im AUTONOMOUS-Modus werden validierte Fixes selbstständig angewendet.
    """

    def __init__(
        self,
        llm: BaseLLMBackend,
        mode: AgentMode = AgentMode.ADVISORY,
        approval_timeout_seconds: int = 3600,
        knowledge_persist_dir: str = "/data/chromadb",
    ) -> None:
        self.llm = llm
        self.mode = mode
        self.approval_timeout_seconds = approval_timeout_seconds
        self.knowledge_persist_dir = knowledge_persist_dir
        self._pending_approvals: dict[str, asyncio.Event] = {}
        self._approval_decisions: dict[str, bool] = {}

    def _build_config(self, agent_type: AgentType) -> AgentRunConfig:
        try:
            from api.routers.agent import get_sandbox_enabled
            dry_run = get_sandbox_enabled()
        except Exception:
            # Fallback: im AUTONOMOUS-Modus kein dry_run, sonst schon
            dry_run = self.mode != AgentMode.AUTONOMOUS
        return AgentRunConfig(
            agent_type=agent_type,
            mode=self.mode,
            dry_run=dry_run,
        )

    async def run_log_analysis(self, raw_logs: list[dict[str, Any]]) -> WorkflowResult:
        """Startet den Log-Analyse-Workflow."""
        from agent_core.agents.log_analysis_agent import LogAnalysisAgent

        config = self._build_config(AgentType.LOG_ANALYSIS)
        agent = LogAnalysisAgent(self.llm, config)
        logger.info("Starte Log-Analyse mit %d Log-Einträgen", len(raw_logs))
        return await agent.run(context={"raw_logs": raw_logs})

    async def run_security_scan(self, ha_config_path: str) -> WorkflowResult:
        """Startet den Security-Audit-Workflow."""
        from agent_core.agents.security_agent import SecurityAgent

        config = self._build_config(AgentType.SECURITY)
        agent = SecurityAgent(self.llm, config)
        logger.info("Starte Security-Scan für Pfad: %s", ha_config_path)
        return await agent.run(context={"ha_config_path": ha_config_path})

    async def run_repair(
        self,
        finding: Any,
        ha_config_path: str,
    ) -> WorkflowResult:
        """Startet den Repair-Workflow für ein einzelnes Finding."""
        from agent_core.agents.repair_agent import RepairAgent

        config = self._build_config(AgentType.REPAIR)
        agent = RepairAgent(self.llm, config)
        logger.info("Starte Repair-Workflow für Finding: %s", getattr(finding, "title", str(finding)))
        return await agent.run(
            context={"finding": finding, "ha_config_path": ha_config_path}
        )

    async def run_validation(self, repair_action: Any) -> WorkflowResult:
        """Startet die Sandbox-Validierung einer RepairAction."""
        from agent_core.agents.validation_agent import ValidationAgent

        config = self._build_config(AgentType.VALIDATION)
        agent = ValidationAgent(self.llm, config)
        return await agent.run(context={"repair_action": repair_action})

    async def run_config_analysis(self, ha_config_path: str) -> WorkflowResult:
        """Startet den Config-Analyse-Workflow."""
        from agent_core.agents.config_analysis_agent import ConfigAnalysisAgent

        config = self._build_config(AgentType.CONFIG_ANALYSIS)
        agent = ConfigAnalysisAgent(self.llm, config)
        logger.info("Starte Config-Analyse für Pfad: %s", ha_config_path)
        return await agent.run(context={"ha_config_path": ha_config_path})

    async def run_knowledge(
        self,
        state_with_findings: Any | None = None,
        query: str | None = None,
    ) -> WorkflowResult:
        """Startet den Knowledge-Agent Workflow (Speichern + Kontext-Anreicherung)."""
        from agent_core.agents.knowledge_agent import KnowledgeAgent

        config = self._build_config(AgentType.KNOWLEDGE)
        agent = KnowledgeAgent(
            self.llm,
            config,
            persist_directory=self.knowledge_persist_dir,
        )
        context: dict[str, Any] = {}
        if query:
            context["query"] = query
        return await agent.run(context=context)

    def get_knowledge_agent(self) -> Any:
        """Gibt eine direkt verwendbare KnowledgeAgent-Instanz zurück."""
        from agent_core.agents.knowledge_agent import KnowledgeAgent

        config = self._build_config(AgentType.KNOWLEDGE)
        return KnowledgeAgent(
            self.llm,
            config,
            persist_directory=self.knowledge_persist_dir,
        )

    async def run_observability_check(
        self,
        ha_client: Any | None = None,
        context: dict[str, Any] | None = None,
    ) -> WorkflowResult:
        """Startet den Observability-Workflow (Post-Apply-Monitoring)."""
        from agent_core.agents.observability_agent import ObservabilityAgent

        config = self._build_config(AgentType.OBSERVABILITY)
        agent = ObservabilityAgent(self.llm, config, ha_client=ha_client)
        logger.info("Starte Observability-Check")
        return await agent.run(context=context or {})

    def get_observability_agent(self, ha_client: Any | None = None) -> Any:
        """Gibt eine direkt verwendbare ObservabilityAgent-Instanz zurück."""
        from agent_core.agents.observability_agent import ObservabilityAgent

        config = self._build_config(AgentType.OBSERVABILITY)
        return ObservabilityAgent(self.llm, config, ha_client=ha_client)

    async def run_full_audit(
        self,
        raw_logs: list[dict[str, Any]] | None = None,
        ha_config_path: str | None = None,
        use_knowledge: bool = True,
    ) -> dict[str, WorkflowResult]:
        """
        Führt einen vollständigen Audit über alle Agenten durch.

        Log-Analyse und Config-Analyse laufen parallel.
        Security-Agent läuft danach mit den kombinierten Findings.
        Knowledge-Agent reichert Findings mit historischem Kontext an.
        """
        tasks: dict[str, asyncio.Task] = {}

        if raw_logs is not None:
            tasks["log_analysis"] = asyncio.create_task(
                self.run_log_analysis(raw_logs)
            )
        if ha_config_path is not None:
            tasks["config_analysis"] = asyncio.create_task(
                self.run_config_analysis(ha_config_path)
            )
            tasks["security"] = asyncio.create_task(
                self.run_security_scan(ha_config_path)
            )

        results: dict[str, WorkflowResult] = {}
        if tasks:
            done = await asyncio.gather(*tasks.values(), return_exceptions=True)
            for key, result in zip(tasks.keys(), done, strict=True):
                if isinstance(result, Exception):
                    logger.error("Agent '%s' fehlgeschlagen: %s", key, result)
                else:
                    results[key] = result  # type: ignore[assignment]

        # Knowledge Agent: Findings aus allen Ergebnissen speichern
        if use_knowledge and results:
            try:
                knowledge_result = await self.run_knowledge()
                results["knowledge"] = knowledge_result
            except Exception as exc:
                logger.warning("Knowledge-Agent fehlgeschlagen (nicht kritisch): %s", exc)

        return results

    async def request_approval(self, repair_action_id: str, summary: str) -> bool:
        """
        Blockiert bis ein menschlicher Operator die Aktion genehmigt oder ablehnt.

        Im ADVISORY-Modus wird immer False zurückgegeben (keine Aktionen).
        Im AUTONOMOUS-Modus wird immer True zurückgegeben (keine Wartezeit).
        """
        if self.mode == AgentMode.ADVISORY:
            return False
        if self.mode == AgentMode.AUTONOMOUS:
            return True

        logger.info(
            "Warte auf Approval für RepairAction %s: %s",
            repair_action_id,
            summary,
        )
        event = asyncio.Event()
        self._pending_approvals[repair_action_id] = event

        try:
            await asyncio.wait_for(event.wait(), timeout=self.approval_timeout_seconds)
            return self._approval_decisions.get(repair_action_id, False)
        except asyncio.TimeoutError:
            logger.warning("Approval für %s abgelaufen", repair_action_id)
            return False
        finally:
            self._pending_approvals.pop(repair_action_id, None)
            self._approval_decisions.pop(repair_action_id, None)

    def submit_approval(self, repair_action_id: str, approved: bool) -> bool:
        """Verarbeitet eine Approval-Entscheidung von der API."""
        event = self._pending_approvals.get(repair_action_id)
        if event is None:
            return False
        self._approval_decisions[repair_action_id] = approved
        event.set()
        return True

    @property
    def pending_approval_ids(self) -> list[str]:
        return list(self._pending_approvals.keys())
