"""API-Router für Repair-Workflows, Approvals und GitOps."""

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/repair", tags=["repair"])

# In-Memory Job-Registry für Background-Repair-Jobs
_repair_jobs: dict[str, dict[str, Any]] = {}


class RepairRequest(BaseModel):
    finding: dict[str, Any]
    ha_config_path: str = "/config"


class ApprovalDecision(BaseModel):
    approved: bool
    reason: str | None = None


class RepairIssuesRequest(BaseModel):
    issues: list[dict[str, Any]]
    ha_config_path: str = "/config"


class RollbackRequest(BaseModel):
    ha_config_path: str
    tag_name: str | None = None
    commit_hash: str | None = None


def _security_issue_to_finding(issue: dict[str, Any]) -> dict[str, Any]:
    """Konvertiert ein SecurityIssue-Dict in ein Finding-kompatibles Dict."""
    check_type = issue.get("check_type", "")
    severity = issue.get("severity", "medium")

    # check_type → FindingCategory mapping
    if any(k in check_type for k in ("injection", "yaml", "jinja", "shell", "eval")):
        category = "config_error"
    elif any(k in check_type for k in ("cve", "supply_chain", "dependency", "docker")):
        category = "security"
    else:
        category = "security"

    file_path = issue.get("file_path") or ""
    return {
        "id": issue.get("id", str(uuid4())),
        "title": issue.get("title", "Security Issue"),
        "description": issue.get("description", ""),
        "severity": severity,
        "category": category,
        "source_agent": "security_scanner",
        "confidence": min(1.0, max(0.0, issue.get("risk_score", 5.0) / 10.0)),
        "affected_files": [file_path] if file_path else [],
        "suggested_fix": issue.get("remediation") or "",
        "risk_score": issue.get("risk_score", 5.0),
        "metadata": {
            "check_type": check_type,
            "line_number": issue.get("line_number"),
            "cve_ids": issue.get("cve_ids", []),
        },
    }


def _build_llm_and_orchestrator():
    from agent_core.orchestrator import AgentOrchestrator
    from config.settings import get_settings
    from llm_backends.base import LLMBackendConfig
    from llm_backends.factory import create_llm_backend
    from models.enums import AgentMode

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
    return AgentOrchestrator(llm, mode=AgentMode(settings.agent.mode))


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def run_repair(request: RepairRequest) -> dict[str, Any]:
    """Startet den 8-stufigen Repair-Workflow für ein einzelnes Finding."""
    from agent_core.repair_store import add_action

    orchestrator = _build_llm_and_orchestrator()
    finding = _security_issue_to_finding(request.finding) if not request.finding.get("category") else request.finding
    result = await orchestrator.run_repair(
        finding=finding,
        ha_config_path=request.ha_config_path,
    )

    repair_action = result.repair_actions[0] if result.repair_actions else None
    action_id = add_action({
        "id": str(result.run_id),
        "title": repair_action.title if repair_action else request.finding.get("title", "Unbekannte Reparatur"),
        "description": repair_action.description if repair_action else request.finding.get("description", ""),
        "rationale": repair_action.rationale if repair_action else "",
        "finding_id": request.finding.get("id", ""),
        "finding_severity": request.finding.get("severity", ""),
        "finding_file": request.finding.get("file_path", ""),
        "finding_line": request.finding.get("line_number"),
        "status": repair_action.status if repair_action else ("proposed" if result.repairs_proposed_count > 0 else "failed"),
        "confidence": repair_action.confidence if repair_action else None,
        "risk_level": repair_action.risk_level if repair_action else "unknown",
        "estimated_impact": repair_action.estimated_impact if repair_action else None,
        "alternatives": repair_action.alternatives if repair_action else [],
        "validation_result": repair_action.validation_result if repair_action else None,
        "simulation_result": repair_action.simulation_result if repair_action else None,
        "changes": [
            {
                "file_path": c.file_path,
                "change_type": c.change_type,
                "diff": c.diff,
            }
            for c in (repair_action.changes if repair_action else [])
        ],
        "context": repair_action.metadata if repair_action else {},
        "repairs_proposed": result.repairs_proposed_count,
        "repairs_applied": result.repairs_applied_count,
        "duration_seconds": result.duration_seconds,
        "errors": result.errors,
        "audit_trail": [str(uid) for uid in result.audit_trail],
        "ha_config_path": request.ha_config_path,
    })

    return {
        "run_id": str(result.run_id),
        "action_id": action_id,
        "success": result.success,
        "repairs_proposed": result.repairs_proposed_count,
        "repairs_applied": result.repairs_applied_count,
        "duration_seconds": result.duration_seconds,
        "errors": result.errors,
    }


_REPAIR_CONCURRENCY = 5  # gleichzeitig laufende Repair-Tasks


async def _repair_single_issue(
    semaphore: asyncio.Semaphore,
    orchestrator: Any,
    issue: dict[str, Any],
    ha_config_path: str,
    job: dict[str, Any],
) -> None:
    """Verarbeitet ein einzelnes Issue mit Semaphore-Begrenzung."""
    from agent_core.repair_store import add_action

    async with semaphore:
        finding = _security_issue_to_finding(issue)
        try:
            result = await orchestrator.run_repair(
                finding=finding,
                ha_config_path=ha_config_path,
            )
            repair_action = result.repair_actions[0] if result.repair_actions else None
            action_id = add_action({
                "id": str(result.run_id),
                "title": repair_action.title if repair_action else issue.get("title", "Unbekannte Reparatur"),
                "description": repair_action.description if repair_action else issue.get("description", ""),
                "rationale": repair_action.rationale if repair_action else "",
                "finding_id": issue.get("id", ""),
                "finding_severity": issue.get("severity", ""),
                "finding_file": issue.get("file_path", ""),
                "finding_line": issue.get("line_number"),
                "remediation_hint": issue.get("remediation", ""),
                "cve_ids": issue.get("cve_ids", []),
                "status": repair_action.status if repair_action else ("proposed" if result.repairs_proposed_count > 0 else "failed"),
                "confidence": repair_action.confidence if repair_action else None,
                "risk_level": repair_action.risk_level if repair_action else "unknown",
                "estimated_impact": repair_action.estimated_impact if repair_action else None,
                "alternatives": repair_action.alternatives if repair_action else [],
                "validation_result": repair_action.validation_result if repair_action else None,
                "simulation_result": repair_action.simulation_result if repair_action else None,
                "changes": [
                    {
                        "file_path": c.file_path,
                        "change_type": c.change_type,
                        "diff": c.diff,
                    }
                    for c in (repair_action.changes if repair_action else [])
                ],
                "context": repair_action.metadata if repair_action else {},
                "repairs_proposed": result.repairs_proposed_count,
                "repairs_applied": result.repairs_applied_count,
                "duration_seconds": result.duration_seconds,
                "errors": result.errors,
                "audit_trail": [str(uid) for uid in result.audit_trail],
                "ha_config_path": ha_config_path,
            })
            job["results"].append({
                "issue_id": issue.get("id", ""),
                "issue_title": issue.get("title", ""),
                "action_id": action_id,
                "run_id": str(result.run_id),
                "success": result.success,
                "repairs_proposed": result.repairs_proposed_count,
                "repairs_applied": result.repairs_applied_count,
                "errors": result.errors,
            })
        except Exception as exc:
            failed_id = add_action({
                "title": issue.get("title", "Unbekannte Reparatur"),
                "description": issue.get("description", ""),
                "finding_id": issue.get("id", ""),
                "finding_severity": issue.get("severity", ""),
                "finding_file": issue.get("file_path", ""),
                "status": "failed",
                "errors": [str(exc)],
                "ha_config_path": ha_config_path,
            })
            job["results"].append({
                "issue_id": issue.get("id", ""),
                "issue_title": issue.get("title", ""),
                "action_id": failed_id,
                "success": False,
                "errors": [str(exc)],
            })
        job["completed"] += 1


async def _run_repair_job(job_id: str, issues: list[dict[str, Any]], ha_config_path: str) -> None:
    """Hintergrund-Task: repariert Issues parallel (max. _REPAIR_CONCURRENCY gleichzeitig)."""
    job = _repair_jobs[job_id]
    orchestrator = _build_llm_and_orchestrator()

    # Vorab-Check: LLM erreichbar? Falls nicht → Job sofort abbrechen statt
    # alle Issues auf Timeout warten zu lassen.
    try:
        llm_available = await orchestrator.llm.health_check()
    except Exception:
        llm_available = False

    if not llm_available:
        logger.warning("Repair-Job %s abgebrochen: kein LLM-Backend erreichbar", job_id)
        job["status"] = "aborted"
        job["error"] = "Kein LLM-Backend erreichbar (weder primär noch Fallback). Job abgebrochen."
        job["finished_at"] = datetime.now(UTC).isoformat()
        return

    semaphore = asyncio.Semaphore(_REPAIR_CONCURRENCY)

    tasks = [
        asyncio.create_task(
            _repair_single_issue(semaphore, orchestrator, issue, ha_config_path, job)
        )
        for issue in issues
    ]
    await asyncio.gather(*tasks, return_exceptions=True)

    job["status"] = "done"
    job["finished_at"] = datetime.now(UTC).isoformat()


@router.post("/issues", status_code=status.HTTP_202_ACCEPTED)
async def repair_issues(request: RepairIssuesRequest) -> dict[str, Any]:
    """Startet den Repair-Workflow für eine Liste von Security-Issues als Hintergrund-Job.

    Gibt sofort eine job_id zurück. Status über GET /repair/jobs/{job_id} abrufen.
    """
    if not request.issues:
        return {"total": 0, "job_id": None, "status": "done", "results": []}

    job_id = str(uuid4())
    _repair_jobs[job_id] = {
        "job_id": job_id,
        "status": "running",
        "total": len(request.issues),
        "completed": 0,
        "results": [],
        "error": None,
        "started_at": datetime.now(UTC).isoformat(),
        "finished_at": None,
    }

    asyncio.create_task(_run_repair_job(job_id, request.issues, request.ha_config_path))

    return {
        "job_id": job_id,
        "total": len(request.issues),
        "status": "running",
    }


@router.get("/jobs/{job_id}")
async def get_repair_job(job_id: str) -> dict[str, Any]:
    """Gibt den aktuellen Status eines Repair-Jobs zurück."""
    job = _repair_jobs.get(job_id)
    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id} nicht gefunden",
        )
    successful = sum(1 for r in job["results"] if r.get("success"))
    return {**job, "successful": successful}


@router.get("/pending")
async def get_pending() -> list[dict[str, Any]]:
    """Gibt alle ausstehenden RepairActions zurück (proposed/simulated/validated)."""
    from agent_core.repair_store import get_pending_actions
    return get_pending_actions()


@router.post("/{repair_action_id}/approve")
async def approve_repair(
    repair_action_id: str,
    decision: ApprovalDecision,
) -> dict[str, Any]:
    """Genehmigt oder lehnt eine RepairAction ab."""
    from agent_core.repair_store import get_action, update_action

    action = get_action(repair_action_id)
    if not action:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"RepairAction {repair_action_id} nicht gefunden",
        )

    new_status = "approved" if decision.approved else "rejected"
    update_action(repair_action_id, {
        "status": new_status,
        "approval_decision": decision.approved,
        "approval_reason": decision.reason,
        "approved_at": datetime.now(UTC).isoformat(),
    })

    return {
        "repair_action_id": repair_action_id,
        "status": new_status,
        "reason": decision.reason,
    }


@router.post("/rollback")
async def rollback(request: RollbackRequest) -> dict[str, Any]:
    """Führt einen GitOps-Rollback auf Tag oder Commit durch."""
    from agent_core.tools.gitops import GitOpsEngine

    gitops = GitOpsEngine(request.ha_config_path)

    if request.tag_name:
        success = gitops.rollback_to_tag(request.tag_name)
        return {"success": success, "rolled_back_to": request.tag_name}
    elif request.commit_hash:
        success = gitops.rollback_to_commit(request.commit_hash)
        return {"success": success, "rolled_back_to": request.commit_hash}
    else:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Entweder tag_name oder commit_hash muss angegeben werden",
        )


@router.get("/history")
async def get_history(
    ha_config_path: str = "/config",
    max_commits: int = 20,
) -> dict[str, Any]:
    """Gibt die abgeschlossenen RepairActions und die Git-History zurück."""
    from agent_core.repair_store import get_history_actions
    from agent_core.tools.gitops import GitOpsEngine

    actions = get_history_actions()
    gitops = GitOpsEngine(ha_config_path)
    git_commits = gitops.get_history(max_commits=max_commits)
    backups = gitops.get_tags()

    return {
        "commits": actions,
        "git_commits": git_commits,
        "backup_tags": backups,
    }


@router.delete("/pending/all")
async def discard_all_pending() -> dict[str, Any]:
    """Verwirft alle ausstehenden RepairActions (proposed/simulated/validated → rejected)."""
    from agent_core.repair_store import get_pending_actions, update_action
    from datetime import UTC, datetime

    pending = get_pending_actions()
    for action in pending:
        update_action(action["id"], {
            "status": "rejected",
            "approval_decision": False,
            "approval_reason": "Manuell verworfen (Bulk-Aktion)",
            "approved_at": datetime.now(UTC).isoformat(),
        })
    return {"discarded": len(pending)}


@router.get("/actions")
async def get_all_actions() -> list[dict[str, Any]]:
    """Gibt alle RepairActions zurück (alle Stati)."""
    from agent_core.repair_store import get_all_actions
    return get_all_actions()
