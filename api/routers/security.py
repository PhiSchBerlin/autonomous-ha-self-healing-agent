"""API-Router für Security-Scans und CVE-Lookup."""

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

router = APIRouter(prefix="/security", tags=["security"])


class SecurityScanRequest(BaseModel):
    ha_config_path: str = "/config"
    check_types: list[str] | None = None


class CVELookupRequest(BaseModel):
    package_name: str
    version: str | None = None


@router.post("/scan", status_code=status.HTTP_202_ACCEPTED)
async def run_security_scan(request: SecurityScanRequest) -> dict[str, Any]:
    """Startet einen Security-Audit: statischer Scan immer, LLM-Scan wenn erreichbar."""
    import time
    from pathlib import Path as _Path
    from security.scanner import scan_directory
    from security.store import set_latest_issues

    t0 = time.monotonic()

    # Pfad prüfen — gibt klaren Fehler statt 500
    if not _Path(request.ha_config_path).exists():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Pfad nicht gefunden: {request.ha_config_path}",
        )

    # Statischer Scan — läuft immer, braucht kein LLM
    static_issues = await scan_directory(request.ha_config_path)
    set_latest_issues(static_issues)

    # LLM-Scan — optional, Fehler werden nicht als 500 weitergegeben
    llm_issues_count = 0
    llm_errors: list[str] = []
    try:
        from config.settings import get_settings
        from llm_backends.base import LLMBackendConfig
        from llm_backends.factory import create_llm_backend
        from models.enums import AgentMode
        from agent_core.orchestrator import AgentOrchestrator

        settings = get_settings()
        llm_config = LLMBackendConfig(
            backend_type=settings.llm.backend_type,
            model=settings.llm.model,
            base_url=settings.llm.base_url,
            api_key=settings.llm.api_key.get_secret_value() if settings.llm.api_key else None,
            fallback_base_url=settings.llm.fallback_base_url,
            fallback_model=settings.llm.fallback_model,
        )
        llm = create_llm_backend(llm_config)
        orchestrator = AgentOrchestrator(llm, mode=AgentMode(settings.agent.mode))
        result = await orchestrator.run_security_scan(request.ha_config_path)
        llm_issues_count = result.security_issues_count
        llm_errors = result.errors
    except Exception as exc:
        llm_errors = [str(exc)]

    return {
        "success": True,
        "static_issues_count": len(static_issues),
        "security_issues_count": llm_issues_count,
        "duration_seconds": time.monotonic() - t0,
        "errors": llm_errors,
    }


@router.post("/cve-lookup")
async def lookup_cve(request: CVELookupRequest) -> dict[str, Any]:
    """Sucht CVEs für ein Python-Paket via OSV.dev."""
    from security.scanner import lookup_cve_osv

    vulns = await lookup_cve_osv(request.package_name, request.version)
    return {
        "package": request.package_name,
        "version": request.version,
        "vulnerabilities": vulns,
        "count": len(vulns),
    }


@router.get("/issues")
async def get_security_issues(
    severity: str | None = None,
    limit: int = 500,
) -> list[dict[str, Any]]:
    """Gibt die zuletzt gefundenen Security-Issues zurück (persistent über Neustarts).

    Query-Parameter:
    - severity: Filtert nach Schweregrad (critical, high, medium, low, info)
    - limit: Maximale Anzahl zurückgegebener Issues (Standard: 500)
    """
    try:
        from security.store import get_latest_issues
        issues = await get_latest_issues()
    except Exception:
        issues = []

    if severity:
        severity_lower = severity.lower()
        issues = [i for i in issues if str(i.severity).lower() == severity_lower]

    issues = issues[:limit]

    return [
        {
            "id": str(i.id),
            "check_type": str(i.check_type),
            "title": i.title,
            "severity": str(i.severity),
            "description": getattr(i, "description", ""),
            "file_path": getattr(i, "file_path", "") or "",
            "line_number": getattr(i, "line_number", None),
            "risk_score": i.risk_score,
            "remediation": getattr(i, "remediation", None),
            "cve_ids": getattr(i, "cve_ids", []),
        }
        for i in issues
    ]


@router.get("/issues/summary")
async def get_security_issues_summary() -> dict[str, Any]:
    """Gibt eine Zusammenfassung der Issues nach Schweregrad zurück."""
    from security.store import get_latest_issues
    try:
        issues = await get_latest_issues()
    except Exception:
        issues = []

    counts: dict[str, int] = {}
    for i in issues:
        sev = str(i.severity).lower()
        counts[sev] = counts.get(sev, 0) + 1

    return {
        "total": len(issues),
        "by_severity": counts,
        "critical": counts.get("critical", 0),
        "high": counts.get("high", 0),
        "medium": counts.get("medium", 0),
        "low": counts.get("low", 0),
        "info": counts.get("info", 0),
    }


@router.post("/quick-scan")
async def quick_scan_content(
    content: str,
    file_type: str = "yaml",
) -> dict[str, Any]:
    """Scannt einen übergebenen Textinhalt direkt (ohne Dateizugriff)."""
    from security.scanner import (
        scan_file_for_jinja2_injection,
        scan_file_for_secrets,
        scan_file_for_shell_injection,
        scan_file_for_yaml_injection,
        scan_python_file,
    )

    issues = []
    if file_type in ("yaml", "yml"):
        issues.extend(scan_file_for_secrets("<inline>", content))
        issues.extend(scan_file_for_yaml_injection("<inline>", content))
        issues.extend(scan_file_for_jinja2_injection("<inline>", content))
        issues.extend(scan_file_for_shell_injection("<inline>", content))
    elif file_type == "python":
        issues.extend(scan_python_file("<inline>", content))
        issues.extend(scan_file_for_secrets("<inline>", content))

    return {
        "issue_count": len(issues),
        "issues": [
            {
                "check_type": str(i.check_type),
                "title": i.title,
                "severity": str(i.severity),
                "line_number": i.line_number,
                "risk_score": i.risk_score,
            }
            for i in issues
        ],
    }
