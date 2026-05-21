"""Datenmodelle für Log-Einträge und Log-Analyse-Ergebnisse."""

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import Field

from .base import TimestampedModel
from .enums import FindingCategory, FindingSeverity, LogSource


class LogEntry(TimestampedModel):
    """Einzelner normalisierter Log-Eintrag aus einer beliebigen Quelle."""

    source: LogSource
    level: str
    message: str
    raw: str
    timestamp: datetime
    component: str | None = None
    integration: str | None = None
    traceback: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LogPattern(TimestampedModel):
    """Erkanntes Muster in Log-Einträgen (Wiederholung, Regression usw.)."""

    pattern_type: str
    description: str
    occurrences: int = 1
    first_seen: datetime
    last_seen: datetime
    affected_entries: list[UUID] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class Finding(TimestampedModel):
    """
    Ein konkretes Problem, das durch Log-Analyse oder Code-Analyse erkannt wurde.

    Findings sind die zentrale Währung des Systems: jeder Agent produziert
    oder konsumiert Findings.
    """

    title: str
    description: str
    severity: FindingSeverity
    category: FindingCategory
    source_agent: str
    confidence: float = Field(ge=0.0, le=1.0)

    affected_files: list[str] = Field(default_factory=list)
    affected_log_entries: list[UUID] = Field(default_factory=list)
    related_findings: list[UUID] = Field(default_factory=list)

    root_cause: str | None = None
    suggested_fix: str | None = None
    explanation: str | None = None

    risk_score: float = Field(default=0.0, ge=0.0, le=10.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
