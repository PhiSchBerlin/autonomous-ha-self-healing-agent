"""API-Router für Repair-Workflows, Approvals und GitOps."""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

router = APIRouter(prefix="/repair", tags=["repair"])


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


@router.post("/run", status_code=status.HTTP_202_ACCEPTED)
async def run_repair(request: RepairRequest) -> dict[str, Any]:
    """Startet den 8-stufigen Repair-Workflow für ein Finding."""
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
    )
    llm = create_llm_backend(llm_config)
    orchestrator = AgentOrchestrator(llm, mode=AgentMode(settings.agent.mode))

    result = await orchestrator.run_repair(
        finding=request.finding,
        ha_config_path=request.ha_config_path,
    )

    return {
        "run_id": str(result.run_id),
        "success": result.success,
        "repairs_proposed": result.repairs_proposed_count,
        "repairs_applied": result.repairs_applied_count,
        "duration_seconds": result.duration_seconds,
        "audit_trail": [str(uid) for uid in result.audit_trail],
        "errors": result.errors,
    }


@router.post("/issues", status_code=status.HTTP_202_ACCEPTED)
async def repair_issues(request: RepairIssuesRequest) -> dict[str, Any]:
    """Startet den Repair-Workflow für eine Liste von Security-Issues.

    Jedes Issue wird sequenziell als Finding an den Orchestrator übergeben.
    Gibt eine Zusammenfassung aller Repair-Läufe zurück.
    """
    from agent_core.orchestrator import AgentOrchestrator
    from config.settings import get_settings
    from llm_backends.base import LLMBackendConfig
    from llm_backends.factory import create_llm_backend
    from models.enums import AgentMode

    if not request.issues:
        return {"total": 0, "results": []}

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

    results = []
    for issue in request.issues:
        try:
            result = await orchestrator.run_repair(
                finding=issue,
                ha_config_path=request.ha_config_path,
            )
            results.append({
                "issue_id": issue.get("id", ""),
                "issue_title": issue.get("title", ""),
                "run_id": str(result.run_id),
                "success": result.success,
                "repairs_proposed": result.repairs_proposed_count,
                "repairs_applied": result.repairs_applied_count,
                "errors": result.errors,
            })
        except Exception as exc:
            results.append({
                "issue_id": issue.get("id", ""),
                "issue_title": issue.get("title", ""),
                "success": False,
                "errors": [str(exc)],
            })

    return {
        "total": len(request.issues),
        "successful": sum(1 for r in results if r.get("success")),
        "results": results,
    }


@router.post("/approve/{repair_action_id}")
async def approve_repair(
    repair_action_id: str,
    decision: ApprovalDecision,
) -> dict[str, Any]:
    """
    Verarbeitet eine Approval-Entscheidung für eine laufende RepairAction.

    Im APPROVAL-Modus pausiert der Repair-Agent und wartet auf diesen Endpoint.
    """
    # Im vollen System: Orchestrator-Instanz aus App-State holen und
    # submit_approval aufrufen um den LangGraph interrupt() zu resumieren.
    return {
        "repair_action_id": repair_action_id,
        "decision": "approved" if decision.approved else "rejected",
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
    """Gibt die Git-History aller Reparaturen zurück."""
    from agent_core.tools.gitops import GitOpsEngine

    gitops = GitOpsEngine(ha_config_path)
    history = gitops.get_history(max_commits=max_commits)
    backups = gitops.get_tags()

    return {
        "commits": history,
        "backup_tags": backups,
    }
