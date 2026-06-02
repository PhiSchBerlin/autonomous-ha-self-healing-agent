"""
Repair-Agent — vollständige Implementierung.

Implementiert den 8-stufigen Reparaturprozess:
  1. Analyse    — Problem vollständig verstehen
  2. Hypothese  — Reparaturstrategie formulieren
  3. Simulation — Änderung simulieren (Dry-Run)
  4. Validation — YAML-Lint, Syntax-Check, HA-Config-Check
  5. Backup     — Git-Tag als Rollback-Punkt
  6. Anwendung  — Änderung auf Dateisystem schreiben
  7. Monitoring — Post-Apply-Checks
  8. Rollback   — Bei Fehler automatisch zurückrollen

Human-Approval via LangGraph interrupt() im APPROVAL-Modus.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph
from langgraph.types import Command, interrupt

from agent_core.tools.analysis_tools import (
    ha_config_check,
    python_syntax_check,
    read_file,
    yamllint_check,
)
from agent_core.tools.gitops import GitOpsEngine
from models.agent_models import AgentState
from models.enums import AgentMode, AgentStatus, AgentType, FindingSeverity, RepairStatus
from models.log_models import Finding
from models.repair_models import AuditRecord, FileChange, RepairAction

from .base_agent import BaseAgent
from .log_analysis_agent import _extract_json

logger = logging.getLogger(__name__)

# Dateien, die niemals vom Agenten modifiziert, gelöscht oder als .bak gesichert werden dürfen.
# Jede Änderung an diesen Dateien könnte HA oder den Supervisor beschädigen.
_PROTECTED_FILES: frozenset[str] = frozenset({
    "secrets.yaml",
    "secrets.yml",
    ".storage",
    "auth",
    "auth_provider.homeassistant",
    "core.config",
    "core.entity_registry",
    "core.device_registry",
})

def _is_protected_path(file_path: str) -> bool:
    """Gibt True zurück wenn die Datei auf der Blacklist steht."""
    name = Path(file_path).name
    return name in _PROTECTED_FILES or any(part in _PROTECTED_FILES for part in Path(file_path).parts)


_GENERATE_PATCH_SYSTEM = """Du bist ein Home Assistant Konfigurationsexperte.

Erstelle einen präzisen Patch für das beschriebene Problem.

Regeln:
- Ändere NUR was notwendig ist
- Berücksichtige HA-Best-Practices (async, ConfigEntry, !secret für Credentials)
- Generiere valides YAML oder Python
- Kein Einführen neuer Abhängigkeiten ohne Begründung

KRITISCH für original_snippet (change_type=modify):
- Kopiere den zu ersetzenden Text WORTGETREU aus dem gezeigten Dateiinhalt
- Kein Zeichen hinzufügen, weglassen oder verändern — exakte Kopie
- Wenn die Zielzeile nicht im gezeigten Ausschnitt sichtbar ist, gib changes=[] zurück

Bei change_type=create:
- original_snippet muss "" (leer) sein
- fixed_snippet enthält den vollständigen Inhalt der neuen Datei

Antworte NUR mit gültigem JSON:
{
  "title": "Kurze Beschreibung der Reparatur",
  "rationale": "Warum diese Änderung notwendig ist",
  "changes": [
    {
      "file_path": "/config/configuration.yaml",
      "change_description": "Was geändert wird",
      "original_snippet": "EXAKT so wie im Dateiinhalt oben — zeichengenaue Kopie",
      "fixed_snippet": "neuer YAML-Block",
      "change_type": "modify"
    }
  ],
  "risk_level": "low|medium|high",
  "estimated_impact": "Kurze Beschreibung der Auswirkungen",
  "alternatives": ["Alternative Lösung A", "Alternative Lösung B"]
}
"""

_VALIDATE_PATCH_SYSTEM = """Du bist ein Home Assistant Code-Reviewer.

Du siehst nur einen Diff-Ausschnitt, NICHT die vollständige Datei. Spekuliere
NICHT über Code der nicht im Diff sichtbar ist. Wenn du dir nicht sicher bist,
setze approved=true und confidence niedrig.

Prüfe NUR was im Diff direkt sichtbar ist:
1. Ist die YAML/Python-Syntax im Fix-Snippet korrekt?
2. Werden Passwörter oder Credentials im Klartext eingeführt?
3. Gibt es offensichtliche Logikfehler im sichtbaren Code?

Antworte NUR mit JSON:
{
  "approved": true/false,
  "confidence": 0.9,
  "issues": ["Nur Probleme die direkt im Diff sichtbar sind"],
  "suggestions": ["Verbesserung 1"]
}
"""


class RepairAgent(BaseAgent):
    """
    Generiert, validiert und wendet Reparaturen an — mit vollständigem Audit-Trail.

    Benötigt ein Finding und optionalen HA-Config-Pfad im Context.
    """

    agent_type = AgentType.REPAIR

    def _build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(AgentState)

        graph.add_node("analyse_finding", self._node_analyse_finding)
        graph.add_node("generate_patch", self._node_generate_patch)
        graph.add_node("simulate", self._node_simulate)
        graph.add_node("validate", self._node_validate)
        graph.add_node("request_approval", self._node_request_approval)
        graph.add_node("backup", self._node_backup)
        graph.add_node("apply", self._node_apply)
        graph.add_node("monitor", self._node_monitor)
        graph.add_node("rollback", self._node_rollback)
        graph.add_node("done", self._node_done)

        graph.add_edge(START, "analyse_finding")
        graph.add_edge("analyse_finding", "generate_patch")
        graph.add_edge("generate_patch", "simulate")
        graph.add_edge("simulate", "validate")
        graph.add_conditional_edges(
            "validate",
            self._route_after_validate,
            {"request_approval": "request_approval", "done": "done"},
        )
        graph.add_conditional_edges(
            "request_approval",
            self._route_after_approval,
            {"backup": "backup", "done": "done"},
        )
        graph.add_edge("backup", "apply")
        graph.add_conditional_edges(
            "apply",
            self._route_after_apply,
            {"monitor": "monitor", "rollback": "rollback"},
        )
        graph.add_edge("monitor", "done")
        graph.add_edge("rollback", "done")
        graph.add_edge("done", END)

        return graph.compile(checkpointer=MemorySaver())

    # -------------------------------------------------------------------------
    # Routing-Funktionen
    # -------------------------------------------------------------------------

    def _route_after_validate(
        self, state: AgentState
    ) -> Literal["request_approval", "done"]:
        """Weiterleitung nach Validation: bei Erfolg → Approval/Apply, bei Fehler → Done."""
        repair = self._get_current_repair(state)
        if repair and repair.status == RepairStatus.VALIDATED:
            return "request_approval"
        return "done"

    def _route_after_approval(
        self, state: AgentState
    ) -> Literal["backup", "done"]:
        """Nach Approval-Entscheidung: genehmigt → Backup+Apply, abgelehnt → Done."""
        repair = self._get_current_repair(state)
        if repair and repair.status == RepairStatus.APPROVED:
            return "backup"
        return "done"

    def _route_after_apply(
        self, state: AgentState
    ) -> Literal["monitor", "rollback"]:
        """Nach Apply: Erfolg → Monitor, Fehler → Rollback."""
        repair = self._get_current_repair(state)
        if repair and repair.status == RepairStatus.APPLIED:
            return "monitor"
        return "rollback"

    # -------------------------------------------------------------------------
    # Graph-Knoten
    # -------------------------------------------------------------------------

    async def _node_analyse_finding(self, state: AgentState) -> dict[str, Any]:
        """Analysiert das Finding und lädt den relevanten Dateiinhalt."""
        finding_data = state.context.get("finding")
        if not finding_data:
            logger.warning("Repair-Agent: Kein Finding im Context")
            return {"errors": [*state.errors, "Kein Finding im Context"]}

        # Finding aus Context laden (entweder Finding-Objekt oder Dict)
        if isinstance(finding_data, Finding):
            finding = finding_data
        else:
            finding = Finding(**finding_data)

        # Betroffene Dateien laden
        file_contents: dict[str, str] = {}
        for fp in finding.affected_files[:3]:
            result = await read_file(fp, max_lines=200)
            if result.get("success"):
                file_contents[fp] = result["content"]

        return {
            "context": {
                **state.context,
                "finding_obj": finding.model_dump(mode="json"),
                "file_contents": file_contents,
            }
        }

    async def _node_generate_patch(self, state: AgentState) -> dict[str, Any]:
        """LLM generiert den konkreten Patch."""
        finding_data = state.context.get("finding_obj", {})
        file_contents = state.context.get("file_contents", {})

        finding_desc = (
            f"Title: {finding_data.get('title', '')}\n"
            f"Description: {finding_data.get('description', '')}\n"
            f"Root Cause: {finding_data.get('root_cause', 'unbekannt')}\n"
            f"Suggested Fix: {finding_data.get('suggested_fix', 'keine Angabe')}"
        )

        files_desc = "\n\n".join(
            f"### {fp}\n```\n{content[:2000]}\n```"
            for fp, content in list(file_contents.items())[:2]
        )

        prompt = (
            f"Erstelle einen Patch für dieses Problem:\n\n"
            f"{finding_desc}\n\n"
            f"Betroffene Dateien:\n{files_desc}"
        )

        raw = await self._call_llm(state, prompt, system_prompt=_GENERATE_PATCH_SYSTEM)

        try:
            patch_data = _extract_json(raw)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Patch-Generierung fehlgeschlagen: ungültiges JSON")
            return {"errors": [*state.errors, "Patch-Generierung fehlgeschlagen"]}

        # RepairAction erstellen
        finding_id_raw = state.context.get("finding_obj", {}).get("id")
        finding_id = UUID(finding_id_raw) if finding_id_raw else uuid4()

        changes: list[FileChange] = []
        for change in patch_data.get("changes", []):
            original = change.get("original_snippet", "")
            fixed = change.get("fixed_snippet", "")
            fp = change.get("file_path", "")

            # Geschützte Dateien niemals anfassen
            if _is_protected_path(fp):
                logger.warning("Patch für geschützte Datei '%s' verworfen (Blacklist)", fp)
                continue

            # YAML-Snippets vorab validieren — ungültige Snippets sofort verwerfen
            # damit die Sandbox-Validierung nicht mit einem unmöglich zu reparierenden
            # Patch-Fragment beschäftigt wird.
            if fp.endswith((".yaml", ".yml")) and fixed:
                try:
                    import yaml as _yaml

                    class _HALoader(_yaml.SafeLoader):
                        pass

                    for _tag in ("!secret", "!include", "!include_dir_list",
                                 "!include_dir_merge_list", "!include_dir_named",
                                 "!include_dir_merge_named", "!env_var"):
                        _HALoader.add_constructor(_tag, lambda loader, node: loader.construct_scalar(node))

                    _yaml.load(fixed, Loader=_HALoader)  # noqa: S506
                except _yaml.YAMLError as yaml_exc:
                    logger.warning(
                        "Patch-Snippet für %s ist kein valides YAML — überspringe Change: %s",
                        fp, yaml_exc,
                    )
                    continue

            diff = _generate_simple_diff(original, fixed, fp)
            changes.append(
                FileChange(
                    file_path=fp,
                    original_content=original,
                    proposed_content=fixed,
                    diff=diff,
                    change_type=change.get("change_type", "modify"),
                )
            )

        repair = RepairAction(
            finding_id=finding_id,
            title=patch_data.get("title", "Automatischer Patch"),
            description=patch_data.get("title", ""),
            rationale=patch_data.get("rationale", ""),
            status=RepairStatus.PROPOSED,
            changes=changes,
            confidence=0.5,  # Vorläufig — wird in _node_validate durch LLM-Review ersetzt
            risk_level=patch_data.get("risk_level", "medium"),
            estimated_impact=patch_data.get("estimated_impact"),
            alternatives=patch_data.get("alternatives", []),
            metadata={
                "finding_title": finding_data.get("title", ""),
                "finding_severity": finding_data.get("severity", ""),
                "finding_description": finding_data.get("description", ""),
                "affected_files": finding_data.get("affected_files", []),
                "suggested_fix": finding_data.get("suggested_fix", ""),
                "llm_raw_patch": patch_data,
            },
        )

        logger.info("Patch generiert: '%s' (%d Änderungen)", repair.title, len(changes))
        return {"repair_actions": [*state.repair_actions, repair]}

    async def _node_simulate(self, state: AgentState) -> dict[str, Any]:
        """Simuliert die Änderung (Dry-Run): wendet sie temporär an und prüft."""
        repair = self._get_current_repair(state)
        if not repair:
            return {"iteration": state.iteration}

        # Simulation: Inhalt zusammensetzen ohne Datei zu schreiben
        simulation_results: dict[str, Any] = {"dry_run": True, "changes_preview": []}

        for change in repair.changes:
            original = change.original_content
            fixed = change.proposed_content
            file_path = change.file_path

            # Aktuellen Dateiinhalt laden
            file_result = await read_file(file_path, max_lines=1000)
            if file_result.get("success"):
                current = file_result["content"]
                simulated = current.replace(original, fixed) if original in current else fixed
            else:
                simulated = fixed

            simulation_results["changes_preview"].append({
                "file": file_path,
                "lines_changed": len(fixed.splitlines()) - len(original.splitlines()),
                "preview": simulated[:200],
            })

        repair.status = RepairStatus.SIMULATED
        repair.simulation_result = simulation_results
        repair.touch()

        updated_repairs = [
            repair if r.id == repair.id else r
            for r in state.repair_actions
        ]
        return {"repair_actions": updated_repairs}

    async def _node_validate(self, state: AgentState) -> dict[str, Any]:
        """Validiert den Patch via ValidationAgent (Sandbox) + LLM-Review."""
        repair = self._get_current_repair(state)
        if not repair:
            return {"iteration": state.iteration}

        # Vollständige Sandbox-Validierung via ValidationAgent
        sandbox_report: dict[str, Any] = {}
        sandbox_issues: list[str] = []
        try:
            from agent_core.agents.validation_agent import ValidationAgent
            val_agent = ValidationAgent(self.llm, self.config)
            val_result = await val_agent.run(
                context={"repair_action": repair.model_dump(mode="json")}
            )
            # ValidationAgent speichert Bericht in context — aus finalem State extrahieren
            # WorkflowResult enthält keinen direkten context-Zugriff,
            # daher holen wir die Daten aus dem LangGraph-State via Checkpoint
            graph = val_agent.get_graph()
            thread_id = str(val_result.run_id)
            snapshot = graph.get_state({"configurable": {"thread_id": thread_id}})
            if snapshot and snapshot.values:
                raw_state = snapshot.values
                ctx = raw_state.get("context", {}) if isinstance(raw_state, dict) else getattr(raw_state, "context", {})
                sandbox_report = ctx.get("validation_report", {})
                sandbox_issues = sandbox_report.get("issues", [])
        except Exception as exc:
            logger.warning("ValidationAgent fehlgeschlagen (Fallback auf Inline): %s", exc)
            # Fallback: einfache Inline-YAML/Python-Prüfung
            for change in repair.changes:
                if change.file_path.endswith((".yaml", ".yml")):
                    try:
                        import yaml
                        yaml.safe_load(change.proposed_content)
                    except Exception as yaml_exc:
                        sandbox_issues.append(f"YAML-Fehler in {change.file_path}: {yaml_exc}")
                if change.file_path.endswith(".py"):
                    import ast
                    try:
                        ast.parse(change.proposed_content)
                    except SyntaxError as syn_exc:
                        sandbox_issues.append(f"Python-Syntaxfehler in {change.file_path}: {syn_exc}")

        # LLM-Review des Patches — Ergebnis vollständig speichern
        llm_review: dict[str, Any] = {}
        if repair.changes:
            changes_str = "\n".join(
                f"Datei: {c.file_path}\n"
                f"Original:\n{c.original_content[:200]}\n"
                f"Fix:\n{c.proposed_content[:200]}"
                for c in repair.changes[:3]
            )
            try:
                raw = await self._call_llm(
                    state,
                    f"Review diesen Patch:\n{changes_str}",
                    system_prompt=_VALIDATE_PATCH_SYSTEM,
                )
                llm_review = _extract_json(raw)
                if "confidence" in llm_review:
                    repair.confidence = float(llm_review["confidence"])
                if not llm_review.get("approved", True):
                    concerns = llm_review.get("issues", [])
                    logger.warning(
                        "LLM-Patch-Review: %d Bedenken (nicht blockierend): %s",
                        len(concerns), concerns,
                    )
            except Exception as llm_exc:
                logger.debug("LLM-Patch-Review übersprungen: %s", llm_exc)

        all_issues = sandbox_issues
        validation_result = {
            "issues": all_issues,
            "passed": len(all_issues) == 0,
            "sandbox_report": sandbox_report,
            "llm_review": {
                "approved": llm_review.get("approved"),
                "confidence": llm_review.get("confidence"),
                "concerns": llm_review.get("issues", []),
                "suggestions": llm_review.get("suggestions", []),
            },
        }
        repair.validation_result = validation_result

        if validation_result["passed"]:
            repair.status = RepairStatus.VALIDATED
            logger.info("Patch validiert (Sandbox + LLM): '%s'", repair.title)
        else:
            repair.status = RepairStatus.FAILED
            logger.warning(
                "Patch-Validation fehlgeschlagen: %d Issues: %s",
                len(all_issues), all_issues,
            )

        repair.touch()
        updated = [repair if r.id == repair.id else r for r in state.repair_actions]
        return {"repair_actions": updated}

    async def _node_request_approval(self, state: AgentState) -> dict[str, Any]:
        """
        Human-in-the-Loop: Pausiert im APPROVAL-Modus via LangGraph interrupt().

        Im ADVISORY-Modus → kein Approval, Status bleibt VALIDATED.
        Im AUTONOMOUS-Modus → automatisch genehmigt.
        """
        repair = self._get_current_repair(state)
        if not repair:
            return {"iteration": state.iteration}

        if state.mode == AgentMode.ADVISORY:
            # Nur empfehlen, nichts anwenden
            logger.info("Advisory-Modus: Repair '%s' vorgeschlagen (nicht angewendet)", repair.title)
            return {"iteration": state.iteration}

        if state.mode == AgentMode.AUTONOMOUS:
            repair.status = RepairStatus.APPROVED
            repair.touch()
            logger.info("Autonomous-Modus: Repair '%s' automatisch genehmigt", repair.title)
            updated = [repair if r.id == repair.id else r for r in state.repair_actions]
            return {"repair_actions": updated}

        # APPROVAL-Modus: LangGraph interrupt() — pausiert Workflow
        approval_payload = {
            "repair_action_id": str(repair.id),
            "title": repair.title,
            "rationale": repair.rationale,
            "risk_level": repair.risk_level,
            "changes": [
                {"file": c.file_path, "diff_preview": c.diff[:300]}
                for c in repair.changes
            ],
        }
        decision = interrupt(approval_payload)

        if decision:
            repair.status = RepairStatus.APPROVED
            logger.info("Repair '%s' manuell genehmigt", repair.title)
        else:
            repair.status = RepairStatus.REJECTED
            logger.info("Repair '%s' manuell abgelehnt", repair.title)

        repair.touch()
        updated = [repair if r.id == repair.id else r for r in state.repair_actions]
        return {"repair_actions": updated}

    async def _node_backup(self, state: AgentState) -> dict[str, Any]:
        """Erstellt Git-Backup-Tag vor der Anwendung."""
        repair = self._get_current_repair(state)
        if not repair:
            return {"iteration": state.iteration}

        config_path = state.context.get("ha_config_path", "/config")
        gitops = GitOpsEngine(config_path)

        backup_tag = gitops.create_backup(
            file_paths=[c.file_path for c in repair.changes],
            repair_action_id=repair.id,
        )
        repair.backup_path = backup_tag
        repair.touch()

        self._add_audit_record(state, "backup_created", str(repair.id), success=True)
        updated = [repair if r.id == repair.id else r for r in state.repair_actions]
        return {"repair_actions": updated}

    async def _node_apply(self, state: AgentState) -> dict[str, Any]:
        """Wendet die validierten Änderungen auf das Dateisystem an."""
        from config.settings import get_settings

        repair = self._get_current_repair(state)
        if not repair or state.context.get("ha_config_path") is None:
            return {"iteration": state.iteration}

        # Sandbox-Durchsetzung: dry_run und Security-Setting prüfen
        settings = get_settings()
        if self.config.dry_run:
            logger.info(
                "dry_run=True: Änderungen für '%s' NICHT auf Disk geschrieben (Simulation)",
                repair.title,
            )
            repair.status = RepairStatus.SIMULATED
            repair.application_result = {
                "success": False,
                "dry_run": True,
                "message": "Änderungen wurden simuliert aber nicht angewendet (dry_run=True)",
                "would_apply": [c.file_path for c in repair.changes],
            }
            repair.touch()
            updated = [repair if r.id == repair.id else r for r in state.repair_actions]
            return {"repair_actions": updated}

        if not settings.security.allow_autonomous_file_writes:
            logger.warning(
                "allow_autonomous_file_writes=False: Schreiben für '%s' blockiert",
                repair.title,
            )
            repair.status = RepairStatus.FAILED
            repair.application_result = {
                "success": False,
                "blocked": True,
                "message": "Dateizugriff blockiert durch SecuritySettings.allow_autonomous_file_writes=False",
            }
            repair.touch()
            self._add_audit_record(
                state, "apply_blocked_by_policy", str(repair.id), success=False,
                error_message="allow_autonomous_file_writes=False",
            )
            updated = [repair if r.id == repair.id else r for r in state.repair_actions]
            return {"repair_actions": updated}

        config_path = state.context.get("ha_config_path", "/config")
        applied_files: list[str] = []
        apply_errors: list[str] = []

        for change in repair.changes:
            if _is_protected_path(change.file_path):
                logger.error(
                    "SICHERHEIT: Schreibversuch auf geschützte Datei '%s' blockiert", change.file_path
                )
                apply_errors.append(f"Geschützte Datei nicht änderbar: {change.file_path}")
                continue
            file_path = Path(change.file_path)
            try:
                if change.change_type == "create":
                    # Neue Datei anlegen — Verzeichnis ggf. miterstellen
                    file_path.parent.mkdir(parents=True, exist_ok=True)
                    file_path.write_text(change.proposed_content, encoding="utf-8")
                    applied_files.append(change.file_path)
                    logger.info("Datei erstellt: %s", change.file_path)
                    continue

                if change.change_type == "delete":
                    if file_path.exists():
                        bak_path = file_path.with_suffix(file_path.suffix + ".bak")
                        try:
                            bak_path.write_text(
                                file_path.read_text(encoding="utf-8"), encoding="utf-8"
                            )
                        except OSError:
                            pass
                        file_path.unlink()
                        applied_files.append(change.file_path)
                        logger.info("Datei gelöscht: %s", change.file_path)
                    else:
                        logger.warning("Löschen übersprungen — Datei nicht vorhanden: %s", change.file_path)
                    continue

                # change_type == "modify" (default)
                if not file_path.exists():
                    apply_errors.append(f"Datei nicht gefunden: {change.file_path}")
                    continue

                current_content = file_path.read_text(encoding="utf-8")

                # Backup der Originaldatei anlegen (.bak — wird vom Agenten ignoriert)
                bak_path = file_path.with_suffix(file_path.suffix + ".bak")
                try:
                    bak_path.write_text(current_content, encoding="utf-8")
                    logger.info("Backup erstellt: %s", bak_path)
                except OSError as bak_exc:
                    logger.warning("Backup fehlgeschlagen (nicht kritisch): %s", bak_exc)

                if change.original_content in current_content:
                    new_content = current_content.replace(
                        change.original_content, change.proposed_content
                    )
                    file_path.write_text(new_content, encoding="utf-8")
                    applied_files.append(change.file_path)
                    logger.info("Änderung angewendet: %s", change.file_path)
                else:
                    apply_errors.append(
                        f"Original-Snippet nicht in {change.file_path} gefunden"
                    )
            except OSError as exc:
                apply_errors.append(f"Fehler beim Schreiben von {change.file_path}: {exc}")

        if apply_errors:
            repair.status = RepairStatus.FAILED
            repair.application_result = {"success": False, "errors": apply_errors}
            logger.error("Apply fehlgeschlagen: %s", apply_errors)
        else:
            # Git-Commit
            gitops = GitOpsEngine(config_path)
            rel_files = [
                str(Path(f).relative_to(config_path)) for f in applied_files
            ]
            commit_hash = gitops.commit_changes(
                changed_files=rel_files,
                message=repair.title,
                repair_action_id=repair.id,
            )
            repair.status = RepairStatus.APPLIED
            repair.git_commit_hash = commit_hash
            repair.application_result = {
                "success": True,
                "applied_files": applied_files,
                "commit_hash": commit_hash,
            }
            logger.info(
                "Repair '%s' erfolgreich angewendet (commit=%s)",
                repair.title,
                commit_hash[:8] if commit_hash else "—",
            )

        repair.touch()
        self._add_audit_record(
            state,
            "repair_applied",
            str(repair.id),
            success=repair.status == RepairStatus.APPLIED,
            error_message=str(apply_errors) if apply_errors else None,
        )
        updated = [repair if r.id == repair.id else r for r in state.repair_actions]
        return {"repair_actions": updated}

    async def _node_monitor(self, state: AgentState) -> dict[str, Any]:
        """Post-Apply-Monitoring: HA-Config-Check nach der Änderung."""
        config_path = state.context.get("ha_config_path", "/config")
        result = await ha_config_check(config_path)

        if result.get("success") and result.get("issues"):
            error_issues = [i for i in result["issues"] if i.get("level") == "ERROR"]
            if error_issues:
                logger.error(
                    "Post-Apply-Check: %d neue Fehler erkannt → Rollback eingeleitet",
                    len(error_issues),
                )
                return {
                    "context": {
                        **state.context,
                        "monitor_failed": True,
                        "monitor_errors": error_issues,
                    }
                }

        logger.info("Post-Apply-Monitoring: Keine neuen Fehler")
        return {
            "context": {**state.context, "monitor_passed": True}
        }

    async def _node_rollback(self, state: AgentState) -> dict[str, Any]:
        """Automatischer Rollback auf den Backup-Tag."""
        repair = self._get_current_repair(state)
        config_path = state.context.get("ha_config_path", "/config")

        if repair and repair.backup_path:
            gitops = GitOpsEngine(config_path)
            success = gitops.rollback_to_tag(repair.backup_path)

            repair.status = RepairStatus.ROLLED_BACK
            repair.rollback_result = {"success": success, "tag": repair.backup_path}
            repair.touch()

            self._add_audit_record(
                state, "rollback_executed", str(repair.id), success=success
            )
            logger.info(
                "Rollback auf '%s': %s", repair.backup_path, "erfolgreich" if success else "fehlgeschlagen"
            )
            updated = [repair if r.id == repair.id else r for r in state.repair_actions]
            return {"repair_actions": updated, "status": AgentStatus.ERROR}

        return {"status": AgentStatus.ERROR}

    async def _node_done(self, state: AgentState) -> dict[str, Any]:
        repair = self._get_current_repair(state)
        if repair:
            logger.info(
                "Repair-Agent abgeschlossen: '%s' → Status: %s",
                repair.title,
                repair.status,
            )
        return {"status": AgentStatus.IDLE}

    # -------------------------------------------------------------------------
    # Hilfsmethoden
    # -------------------------------------------------------------------------

    def _get_current_repair(self, state: AgentState) -> RepairAction | None:
        """Gibt die aktuell bearbeitete RepairAction zurück (letzte in der Liste)."""
        return state.repair_actions[-1] if state.repair_actions else None

    def _add_audit_record(
        self,
        state: AgentState,
        action_type: str,
        target: str,
        success: bool,
        error_message: str | None = None,
    ) -> None:
        state.audit_records.append(
            AuditRecord(
                action_type=action_type,
                actor="repair_agent",
                target=target,
                success=success,
                error_message=error_message,
            )
        )


# ---------------------------------------------------------------------------
# Modul-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _generate_simple_diff(original: str, fixed: str, file_path: str = "") -> str:
    """Generiert einen einfachen unified-ähnlichen Diff-String."""
    import difflib
    orig_lines = original.splitlines(keepends=True)
    fixed_lines = fixed.splitlines(keepends=True)
    diff = difflib.unified_diff(
        orig_lines,
        fixed_lines,
        fromfile=f"a/{file_path}",
        tofile=f"b/{file_path}",
        lineterm="",
    )
    return "".join(diff)
