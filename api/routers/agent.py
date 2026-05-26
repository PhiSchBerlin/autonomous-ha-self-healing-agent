"""API-Router für Agent-Steuerung und Ergebnis-Abfrage."""

import asyncio
import logging
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)

# Laufende und abgeschlossene Audit-Jobs (in-memory, reicht für Single-Instance)
_audit_jobs: dict[str, dict[str, Any]] = {}

router = APIRouter(prefix="/agents", tags=["agents"])


class RunLogAnalysisRequest(BaseModel):
    raw_logs: list[dict[str, Any]]
    mode: str = "advisory"


class FullAuditRequest(BaseModel):
    raw_logs: list[dict[str, Any]] | None = None
    ha_config_path: str = "/config"


class ApprovalDecisionRequest(BaseModel):
    approved: bool
    reason: str | None = None


@router.post("/log-analysis", status_code=status.HTTP_202_ACCEPTED)
async def run_log_analysis(request: RunLogAnalysisRequest) -> dict[str, Any]:
    """Startet einen Log-Analyse-Workflow asynchron."""
    from agent_core.orchestrator import AgentOrchestrator
    from config.settings import get_settings
    from llm_backends.factory import create_llm_backend
    from models.enums import AgentMode, LLMBackendType
    from llm_backends.base import LLMBackendConfig

    settings = get_settings()
    llm_config = LLMBackendConfig(
        backend_type=settings.llm.backend_type,
        model=settings.llm.model,
        base_url=settings.llm.base_url,
        api_key=settings.llm.api_key.get_secret_value() if settings.llm.api_key else None,
    )
    llm = create_llm_backend(llm_config)

    try:
        mode = AgentMode(request.mode)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Unbekannter Modus: {request.mode}",
        )

    orchestrator = AgentOrchestrator(llm, mode=mode)
    result = await orchestrator.run_log_analysis(request.raw_logs)

    return {
        "run_id": str(result.run_id),
        "success": result.success,
        "findings_count": result.findings_count,
        "duration_seconds": result.duration_seconds,
        "errors": result.errors,
    }


@router.post("/approvals/{repair_action_id}")
async def submit_approval(
    repair_action_id: str,
    decision: ApprovalDecisionRequest,
) -> dict[str, str]:
    """Verarbeitet eine Approval-Entscheidung für eine ausstehende RepairAction."""
    # Im vollen System wird der Orchestrator als Dependency injiziert
    return {
        "repair_action_id": repair_action_id,
        "status": "approved" if decision.approved else "rejected",
    }


async def _run_full_audit_background(job_id: str, req: FullAuditRequest) -> None:
    """Führt den Full Audit im Hintergrund aus und speichert das Ergebnis."""
    from agent_core.orchestrator import AgentOrchestrator
    from config.settings import get_settings
    from llm_backends.base import LLMBackendConfig
    from llm_backends.factory import create_llm_backend
    from models.enums import AgentMode

    _audit_jobs[job_id]["status"] = "running"
    try:
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
        results = await orchestrator.run_full_audit(
            raw_logs=req.raw_logs,
            ha_config_path=req.ha_config_path,
            use_knowledge=True,
        )
        audit_result = {
            "agents_run": list(results.keys()),
            **{k: {"run_id": str(v.run_id), "success": v.success} for k, v in results.items()},
        }
        _audit_jobs[job_id]["status"] = "completed"
        _audit_jobs[job_id]["result"] = audit_result
        try:
            from security.audit_store import set_latest_audit
            set_latest_audit({"job_id": job_id, "status": "completed", "result": audit_result})
        except Exception as store_exc:
            logger.warning("Audit-Ergebnis konnte nicht persistiert werden: %s", store_exc)
    except Exception as exc:
        logger.error("Full Audit Job %s fehlgeschlagen: %s", job_id, exc)
        _audit_jobs[job_id]["status"] = "failed"
        _audit_jobs[job_id]["error"] = str(exc)


@router.post("/full-audit", status_code=status.HTTP_202_ACCEPTED)
async def run_full_audit(
    background_tasks: BackgroundTasks,
    request: FullAuditRequest | None = None,
) -> dict[str, Any]:
    """Startet Log-Analyse, Config-Analyse und Security-Scan als Hintergrund-Job."""
    req = request or FullAuditRequest()
    job_id = str(uuid4())
    _audit_jobs[job_id] = {"status": "queued", "result": None, "error": None}
    background_tasks.add_task(_run_full_audit_background, job_id, req)
    return {"job_id": job_id, "status": "queued"}


@router.get("/full-audit/latest")
async def get_latest_audit() -> dict[str, Any]:
    """Gibt das zuletzt abgeschlossene Audit-Ergebnis zurück (persistent über Neustarts)."""
    from security.audit_store import get_latest_audit
    result = get_latest_audit()
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Noch kein Audit gelaufen")
    return result


@router.get("/full-audit/{job_id}")
async def get_full_audit_status(job_id: str) -> dict[str, Any]:
    """Gibt den Status und das Ergebnis eines laufenden oder abgeschlossenen Audits zurück."""
    job = _audit_jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Job nicht gefunden")
    return {"job_id": job_id, **job}


@router.get("/health")
async def health() -> dict[str, Any]:
    from config.settings import get_settings

    settings = get_settings()
    return {
        "status": "ok",
        "mode": settings.agent.mode,
        "pending_approvals": 0,
    }
