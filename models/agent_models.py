"""Datenmodelle für Agent-Zustand und Orchestrierung."""

from typing import Any
from uuid import UUID

from pydantic import Field

from .base import TimestampedModel
from .enums import AgentMode, AgentStatus, AgentType
from .log_models import Finding, LogEntry
from .repair_models import AuditRecord, RepairAction
from .security_models import SecurityIssue


class AgentRunConfig(TimestampedModel):
    """Konfiguration für einen einzelnen Agent-Lauf."""

    agent_type: AgentType
    mode: AgentMode = AgentMode.ADVISORY
    max_iterations: int = 10
    timeout_seconds: int = 300
    dry_run: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class AgentState(TimestampedModel):
    """
    Der vollständige, persistierte Zustand eines Agent-Workflows in LangGraph.

    Dieser State wird an jeden Graphknoten übergeben und enthält alle
    akkumulierten Ergebnisse des laufenden Workflows.
    """

    run_id: UUID
    agent_type: AgentType
    status: AgentStatus = AgentStatus.IDLE
    mode: AgentMode = AgentMode.ADVISORY
    iteration: int = 0

    log_entries: list[LogEntry] = Field(default_factory=list)
    findings: list[Finding] = Field(default_factory=list)
    security_issues: list[SecurityIssue] = Field(default_factory=list)
    repair_actions: list[RepairAction] = Field(default_factory=list)
    audit_records: list[AuditRecord] = Field(default_factory=list)

    pending_approval_ids: list[UUID] = Field(default_factory=list)

    messages: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    context: dict[str, Any] = Field(default_factory=dict)
    memory_namespace: str | None = None


class WorkflowResult(TimestampedModel):
    """Zusammenfassung eines abgeschlossenen Agent-Workflows."""

    run_id: UUID
    agent_type: AgentType
    success: bool

    findings_count: int = 0
    security_issues_count: int = 0
    repairs_applied_count: int = 0
    repairs_proposed_count: int = 0

    duration_seconds: float = 0.0
    llm_calls: int = 0
    tool_calls: int = 0

    summary: str = ""
    errors: list[str] = Field(default_factory=list)
    audit_trail: list[UUID] = Field(default_factory=list)

    repair_actions: list["RepairAction"] = Field(default_factory=list)
