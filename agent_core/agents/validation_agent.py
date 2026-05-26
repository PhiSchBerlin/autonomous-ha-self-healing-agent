"""
Validation-Agent — Sandbox-Validierung vor jeder Reparatur.

Führt alle verfügbaren statischen Prüfungen durch bevor eine Änderung
angewendet wird:
- YAML-Lint
- Python-Syntax
- HA Config Check
- Jinja2-Template-Validierung
- Diff-Plausibilitätsprüfung
- LLM-basierter Sanity-Check

Workflow:
  prepare_sandbox → lint_yaml → check_python → check_ha → validate_templates
                 → plausibility_check → generate_validation_report → done
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_core.tools.analysis_tools import (
    ha_config_check,
    jinja2_syntax_check,
    python_syntax_check,
    yamllint_check,
)
from models.agent_models import AgentState
from models.enums import AgentStatus, AgentType
from models.repair_models import RepairAction

from .base_agent import BaseAgent
from .log_analysis_agent import _extract_json

logger = logging.getLogger(__name__)

_SANITY_CHECK_SYSTEM = """Du bist ein Home Assistant Code-Reviewer.

Beurteile ob die vorgeschlagene Änderung sicher angewendet werden kann.
Prüfe:
1. Ist die Änderung minimal und fokussiert?
2. Werden keine unerwarteten Seiteneffekte eingeführt?
3. Ist der Kontext korrekt (richtige Einrückung, YAML-Struktur)?
4. Gibt es offensichtliche Logikfehler?

Antworte NUR mit JSON:
{
  "safe_to_apply": true/false,
  "confidence": 0.95,
  "concerns": ["Bedenken 1", "Bedenken 2"],
  "verdict": "Kurze Bewertung"
}
"""


class ValidationAgent(BaseAgent):
    """
    Validiert RepairActions in einer isolierten Sandbox bevor sie angewendet werden.

    Schreibt niemals in das Produktivsystem — nur temporäre Dateien.
    """

    agent_type = AgentType.VALIDATION

    def _build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(AgentState)

        graph.add_node("prepare_sandbox", self._node_prepare_sandbox)
        graph.add_node("lint_yaml", self._node_lint_yaml)
        graph.add_node("check_python", self._node_check_python)
        graph.add_node("check_ha_config", self._node_check_ha_config)
        graph.add_node("validate_templates", self._node_validate_templates)
        graph.add_node("plausibility_check", self._node_plausibility_check)
        graph.add_node("generate_report", self._node_generate_report)
        graph.add_node("done", self._node_done)

        graph.add_edge(START, "prepare_sandbox")
        graph.add_edge("prepare_sandbox", "lint_yaml")
        graph.add_edge("lint_yaml", "check_python")
        graph.add_edge("check_python", "check_ha_config")
        graph.add_edge("check_ha_config", "validate_templates")
        graph.add_edge("validate_templates", "plausibility_check")
        graph.add_edge("plausibility_check", "generate_report")
        graph.add_edge("generate_report", "done")
        graph.add_edge("done", END)

        return graph.compile(checkpointer=MemorySaver())

    async def _node_prepare_sandbox(self, state: AgentState) -> dict[str, Any]:
        """Erstellt temporäre Sandbox-Dateien für alle vorgeschlagenen Änderungen."""
        from sandbox import SandboxManager

        repair_data = state.context.get("repair_action")
        if not repair_data:
            return {"errors": [*state.errors, "Kein repair_action im Context"]}

        repair = RepairAction(**repair_data) if isinstance(repair_data, dict) else repair_data

        sandbox = SandboxManager.from_repair_action(repair)
        # initialize() statt __enter__() — Cleanup in _node_generate_report via
        # shutil.rmtree, weil der Context-Manager die Knotengrenze nicht überlebt.
        sandbox.initialize()

        logger.info(
            "Sandbox erstellt: %d Dateien in %s", len(sandbox.files), sandbox.directory
        )
        return {
            "context": {
                **state.context,
                "sandbox_dir": sandbox.directory,
                "sandbox_files": sandbox.files,
                "repair_obj": repair.model_dump(mode="json"),
                "validation_issues": [],
                "validation_passed": False,
            }
        }

    async def _node_lint_yaml(self, state: AgentState) -> dict[str, Any]:
        """YAML-Lint auf alle Sandbox-YAML-Dateien."""
        sandbox_files: dict[str, str] = state.context.get("sandbox_files", {})
        issues: list[str] = list(state.context.get("validation_issues", []))

        for orig_path, sandbox_path in sandbox_files.items():
            if not sandbox_path.endswith((".yaml", ".yml")):
                continue
            result = await yamllint_check(sandbox_path)
            for issue in result.get("issues", []):
                if issue.get("level") == "error":
                    issues.append(
                        f"YAML-Fehler in {Path(orig_path).name} "
                        f"Zeile {issue.get('line', '?')}: {issue.get('message', '')}"
                    )

            # Zusätzlich: pyyaml safe_load
            try:
                import yaml
                with open(sandbox_path) as f:
                    yaml.safe_load(f)
            except Exception as exc:
                issues.append(f"YAML-Parse-Fehler in {Path(orig_path).name}: {exc}")

        return {"context": {**state.context, "validation_issues": issues}}

    async def _node_check_python(self, state: AgentState) -> dict[str, Any]:
        """Python-Syntax-Check auf alle .py-Sandbox-Dateien."""
        sandbox_files: dict[str, str] = state.context.get("sandbox_files", {})
        issues: list[str] = list(state.context.get("validation_issues", []))

        for orig_path, sandbox_path in sandbox_files.items():
            if not sandbox_path.endswith(".py"):
                continue
            result = await python_syntax_check(sandbox_path)
            for issue in result.get("issues", []):
                issues.append(
                    f"Python-Syntaxfehler in {Path(orig_path).name} "
                    f"Zeile {issue.get('line', '?')}: {issue.get('message', '')}"
                )

        return {"context": {**state.context, "validation_issues": issues}}

    async def _node_check_ha_config(self, state: AgentState) -> dict[str, Any]:
        """HA Config Check auf die Sandbox (wenn möglich)."""
        sandbox_dir = state.context.get("sandbox_dir", "")
        issues: list[str] = list(state.context.get("validation_issues", []))

        if sandbox_dir:
            result = await ha_config_check(sandbox_dir)
            if result.get("success"):
                for issue in result.get("issues", []):
                    if issue.get("level") == "ERROR":
                        issues.append(f"HA Config Check Fehler: {issue.get('message', '')}")
            else:
                logger.debug("HA Config Check nicht verfügbar in Sandbox: %s", result.get("error"))

        return {"context": {**state.context, "validation_issues": issues}}

    async def _node_validate_templates(self, state: AgentState) -> dict[str, Any]:
        """Jinja2-Template-Validierung in allen YAML-Sandbox-Dateien."""
        import re

        sandbox_files: dict[str, str] = state.context.get("sandbox_files", {})
        issues: list[str] = list(state.context.get("validation_issues", []))
        template_re = re.compile(r"_template:\s*['\"]?(.+?)['\"]?\s*$", re.MULTILINE)

        for orig_path, sandbox_path in sandbox_files.items():
            if not sandbox_path.endswith((".yaml", ".yml")):
                continue
            try:
                content = Path(sandbox_path).read_text(encoding="utf-8")
            except OSError:
                continue

            for m in template_re.finditer(content):
                tmpl = m.group(1).strip()
                if "{{" not in tmpl and "{%" not in tmpl:
                    continue
                result = await jinja2_syntax_check(tmpl)
                if not result.get("valid", True):
                    lineno = content[: m.start()].count("\n") + 1
                    issues.append(
                        f"Jinja2-Fehler in {Path(orig_path).name} "
                        f"Zeile {lineno}: {result.get('error', '')}"
                    )

        return {"context": {**state.context, "validation_issues": issues}}

    async def _node_plausibility_check(self, state: AgentState) -> dict[str, Any]:
        """LLM-basierter Sanity-Check: Beurteilt ob die Änderung sicher anzuwenden ist."""
        repair_data = state.context.get("repair_obj", {})
        if not repair_data:
            return {}

        changes_summary = "\n".join(
            f"- {c.get('file_path', '')}: {c.get('change_type', 'modify')}\n"
            f"  Diff: {c.get('diff', '')[:200]}"
            for c in repair_data.get("changes", [])[:3]
        )

        prompt = (
            f"Bewerte diese Konfigurationsänderung:\n\n"
            f"Title: {repair_data.get('title', '')}\n"
            f"Rationale: {repair_data.get('rationale', '')}\n"
            f"Risk Level: {repair_data.get('risk_level', 'unknown')}\n\n"
            f"Änderungen:\n{changes_summary}"
        )

        raw = await self._call_llm(state, prompt, system_prompt=_SANITY_CHECK_SYSTEM)
        issues: list[str] = list(state.context.get("validation_issues", []))

        try:
            review = _extract_json(raw)
            if not review.get("safe_to_apply", True):
                concerns = review.get("concerns", [])
                issues.extend(concerns)
                logger.warning(
                    "LLM-Sanity-Check: Nicht sicher anzuwenden — %d Bedenken", len(concerns)
                )
            return {
                "context": {
                    **state.context,
                    "validation_issues": issues,
                    "llm_sanity": review,
                }
            }
        except (json.JSONDecodeError, ValueError):
            return {"context": {**state.context, "validation_issues": issues}}

    async def _node_generate_report(self, state: AgentState) -> dict[str, Any]:
        """Erstellt den finalen Validierungsreport."""
        issues: list[str] = state.context.get("validation_issues", [])
        passed = len(issues) == 0
        llm_sanity = state.context.get("llm_sanity", {})

        report = {
            "passed": passed,
            "issue_count": len(issues),
            "issues": issues,
            "llm_verdict": llm_sanity.get("verdict", ""),
            "llm_confidence": llm_sanity.get("confidence", 0.0),
        }

        logger.info(
            "Validation abgeschlossen: %s (%d Issues)",
            "PASSED" if passed else "FAILED",
            len(issues),
        )

        # Sandbox aufräumen
        sandbox_dir = state.context.get("sandbox_dir", "")
        if sandbox_dir:
            import shutil
            try:
                shutil.rmtree(sandbox_dir, ignore_errors=True)
            except Exception:
                pass

        return {
            "context": {**state.context, "validation_report": report},
        }

    async def _node_done(self, state: AgentState) -> dict[str, Any]:
        return {"status": AgentStatus.IDLE}
