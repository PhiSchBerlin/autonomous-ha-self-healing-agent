"""API-Router für Repair-Workflows, Approvals und GitOps."""

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

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

    action_id = add_action({
        "id": str(result.run_id),
        "title": request.finding.get("title", "Unbekannte Reparatur"),
        "description": request.finding.get("description", ""),
        "finding_id": request.finding.get("id", ""),
        "finding_severity": request.finding.get("severity", ""),
        "finding_file": request.finding.get("file_path", ""),
        "finding_line": request.finding.get("line_number"),
        "status": "proposed" if result.repairs_proposed_count > 0 else "failed",
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


@router.post("/issues", status_code=status.HTTP_202_ACCEPTED)
async def repair_issues(request: RepairIssuesRequest) -> dict[str, Any]:
    """Startet den Repair-Workflow für eine Liste von Security-Issues sequenziell."""
    from agent_core.repair_store import add_action

    if not request.issues:
        return {"total": 0, "results": []}

    orchestrator = _build_llm_and_orchestrator()
    results = []

    for issue in request.issues:
        finding = _security_issue_to_finding(issue)
        try:
            result = await orchestrator.run_repair(
                finding=finding,
                ha_config_path=request.ha_config_path,
            )
            action_id = add_action({
                "id": str(result.run_id),
                "title": issue.get("title", "Unbekannte Reparatur"),
                "description": issue.get("description", ""),
                "finding_id": issue.get("id", ""),
                "finding_severity": issue.get("severity", ""),
                "finding_file": issue.get("file_path", ""),
                "finding_line": issue.get("line_number"),
                "remediation_hint": issue.get("remediation", ""),
                "cve_ids": issue.get("cve_ids", []),
                "status": "proposed" if result.repairs_proposed_count > 0 else "failed",
                "repairs_proposed": result.repairs_proposed_count,
                "repairs_applied": result.repairs_applied_count,
                "duration_seconds": result.duration_seconds,
                "errors": result.errors,
                "audit_trail": [str(uid) for uid in result.audit_trail],
                "ha_config_path": request.ha_config_path,
            })
            results.append({
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
                "ha_config_path": request.ha_config_path,
            })
            results.append({
                "issue_id": issue.get("id", ""),
                "issue_title": issue.get("title", ""),
                "action_id": failed_id,
                "success": False,
                "errors": [str(exc)],
            })

    return {
        "total": len(request.issues),
        "successful": sum(1 for r in results if r.get("success")),
        "results": results,
    }


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


@router.get("/actions")
async def get_all_actions() -> list[dict[str, Any]]:
    """Gibt alle RepairActions zurück (alle Stati)."""
    from agent_core.repair_store import get_all_actions
    return get_all_actions()
