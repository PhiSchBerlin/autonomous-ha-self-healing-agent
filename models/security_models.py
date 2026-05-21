"""Datenmodelle für Security-Analyse und CVE-Tracking."""

from typing import Any

from pydantic import Field

from .base import TimestampedModel
from .enums import FindingSeverity, SecurityCheckType


class SecurityIssue(TimestampedModel):
    """
    Ein konkretes Sicherheitsproblem, das vom Security-Agent erkannt wurde.

    Enthält alle Informationen für die Risikobewertung und Remediation.
    """

    check_type: SecurityCheckType
    title: str
    description: str
    severity: FindingSeverity

    file_path: str | None = None
    line_number: int | None = None
    code_snippet: str | None = None

    risk_score: float = Field(ge=0.0, le=10.0)
    cvss_score: float | None = Field(default=None, ge=0.0, le=10.0)
    cve_ids: list[str] = Field(default_factory=list)

    remediation: str | None = None
    references: list[str] = Field(default_factory=list)

    false_positive: bool = False
    suppressed: bool = False
    suppression_reason: str | None = None

    metadata: dict[str, Any] = Field(default_factory=dict)


class CVEEntry(TimestampedModel):
    """Eintrag aus einer CVE-Datenbank für bekannte Schwachstellen."""

    cve_id: str
    description: str
    severity: FindingSeverity
    cvss_score: float | None = Field(default=None, ge=0.0, le=10.0)
    cvss_vector: str | None = None

    affected_packages: list[str] = Field(default_factory=list)
    affected_versions: list[str] = Field(default_factory=list)
    fixed_in_versions: list[str] = Field(default_factory=list)

    published_at: str | None = None
    modified_at: str | None = None
    references: list[str] = Field(default_factory=list)


class SecurityReport(TimestampedModel):
    """Vollständiger Security-Audit-Bericht für eine HA-Instanz."""

    issues: list[SecurityIssue] = Field(default_factory=list)
    cve_matches: list[CVEEntry] = Field(default_factory=list)

    overall_risk_score: float = Field(ge=0.0, le=10.0, default=0.0)
    critical_count: int = 0
    high_count: int = 0
    medium_count: int = 0
    low_count: int = 0

    scanned_files: list[str] = Field(default_factory=list)
    scan_duration_seconds: float = 0.0

    summary: str = ""
    recommendations: list[str] = Field(default_factory=list)
