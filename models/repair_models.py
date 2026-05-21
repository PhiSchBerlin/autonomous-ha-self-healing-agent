"""Datenmodelle für Reparaturaktionen, Validierung und Rollback."""

from typing import Any
from uuid import UUID

from pydantic import Field

from .base import TimestampedModel
from .enums import ApprovalStatus, RepairStatus


class FileChange(TimestampedModel):
    """Eine einzelne Dateiänderung als Teil einer RepairAction."""

    file_path: str
    original_content: str
    proposed_content: str
    diff: str
    change_type: str = "modify"  # modify | create | delete


class RepairAction(TimestampedModel):
    """
    Eine vollständige Reparaturaktion mit Backup, Diff und Validierungsstatus.

    Durchläuft zwingend: proposed → simulated → validated → approved → applied.
    Rollback ist jederzeit möglich.
    """

    finding_id: UUID
    title: str
    description: str
    rationale: str

    status: RepairStatus = RepairStatus.PROPOSED
    changes: list[FileChange] = Field(default_factory=list)

    simulation_result: dict[str, Any] | None = None
    validation_result: dict[str, Any] | None = None
    application_result: dict[str, Any] | None = None
    rollback_result: dict[str, Any] | None = None

    backup_path: str | None = None
    git_commit_hash: str | None = None

    confidence: float = Field(ge=0.0, le=1.0)
    risk_level: str = "low"  # low | medium | high | critical
    estimated_impact: str | None = None

    alternatives: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(TimestampedModel):
    """
    Anfrage an einen menschlichen Operator zur Genehmigung einer RepairAction.

    Wird im APPROVAL-Modus erstellt und muss vor Ausführung bestätigt werden.
    """

    repair_action_id: UUID
    status: ApprovalStatus = ApprovalStatus.PENDING

    requested_by: str
    approved_by: str | None = None
    rejection_reason: str | None = None

    expires_at: str | None = None
    notification_sent: bool = False

    summary: str = ""
    risk_summary: str = ""


class AuditRecord(TimestampedModel):
    """
    Unveränderlicher Audit-Eintrag für jede Systemaktion.

    Jede Änderung am System erzeugt einen AuditRecord für vollständige
    Nachvollziehbarkeit und Compliance.
    """

    action_type: str
    actor: str
    target: str | None = None

    repair_action_id: UUID | None = None
    finding_id: UUID | None = None

    before_state: dict[str, Any] | None = None
    after_state: dict[str, Any] | None = None

    success: bool
    error_message: str | None = None

    rationale: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)
