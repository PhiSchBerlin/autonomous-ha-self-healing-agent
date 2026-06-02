"""
Config-Analyse-Agent — vollständige Implementierung.

Analysiert alle relevanten Home Assistant Konfigurationsdateien:
- configuration.yaml, packages/, secrets.yaml
- automations.yaml, scripts.yaml, scenes.yaml
- Blueprints (YAML)
- Custom Components (Python + manifest.json)
- ESPHome YAML
- Lovelace / Dashboard YAML

Workflow:
  discover_files → lint_yaml → check_ha_config → analyze_automations
                → analyze_templates → analyze_custom_components
                → generate_findings → done
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Literal

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_core.tools.analysis_tools import (
    grep_pattern,
    ha_config_check,
    jinja2_syntax_check,
    list_ha_files,
    python_syntax_check,
    read_file,
    yamllint_check,
)
from models.agent_models import AgentState
from models.enums import AgentMode, AgentStatus, AgentType, FindingCategory, FindingSeverity
from models.log_models import Finding

from .base_agent import BaseAgent
from .log_analysis_agent import _calculate_risk_scores, _deduplicate_findings, _extract_json

logger = logging.getLogger(__name__)

# HA-spezifische Deprecation-Muster (Stand 2024/2025)
_DEPRECATED_PATTERNS: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\bplatform:\s+mqtt\b"),
        "MQTT platform (deprecated)",
        "Migrate to modern MQTT entities without 'platform: mqtt'",
    ),
    (
        re.compile(r"\bwhitelist\b|\bblacklist\b"),
        "Deprecated allow/denylist terminology",
        "Replace 'whitelist'/'blacklist' with 'allowlist'/'denylist'",
    ),
    (
        re.compile(r"\bevent_data_template\b"),
        "event_data_template deprecated",
        "Use 'event_data' with Jinja2 templates directly",
    ),
    (
        re.compile(r"\bservice_template\b"),
        "service_template deprecated",
        "Use 'action' with templated values",
    ),
    (
        re.compile(r"\bdata_template\b"),
        "data_template deprecated",
        "Use 'data' with Jinja2 templates directly",
    ),
    (
        re.compile(r"\bent_pic\b|\bentity_picture_template\b"),
        "entity_picture_template deprecated",
        "Use 'entity_picture' with template",
    ),
    (
        re.compile(r"homeassistant\.turn_on|homeassistant\.turn_off", re.I),
        "homeassistant.turn_on/off deprecated in automations",
        "Use specific domain actions (e.g. light.turn_on)",
    ),
]

# ESPHome-spezifische Deprecation-Muster
_ESPHOME_DEPRECATED: list[tuple[re.Pattern[str], str, str]] = [
    (
        re.compile(r"\bdallas\b"),
        "ESPHome 'dallas' platform deprecated",
        "Migrate to 'one_wire' platform (ESPHome >= 2023.9)",
    ),
    (
        re.compile(r"\bota:\s*$", re.M),
        "ESPHome bare 'ota:' deprecated",
        "Use 'ota:\n  platform: esphome' (ESPHome >= 2024.6)",
    ),
]

_ANALYZE_AUTOMATION_SYSTEM = """Du bist ein Home Assistant Automatisierungs-Experte.

Analysiere die gegebene Automatisierungs-YAML auf:
1. Logikfehler (falsche Trigger, fehlende Conditions)
2. Deprecated Patterns (data_template, service_template usw.)
3. Fehlende mode: (queued/single/restart/parallel)
4. Unvollständige entity_id Referenzen
5. Template-Fehler in value_template, trigger templates
6. Ineffiziente Strukturen (zu viele Trigger, fehlende aliases)

Antworte NUR mit gültigem JSON:
{
  "findings": [
    {
      "title": "Kurzer Titel",
      "description": "Detaillierte Beschreibung",
      "severity": "critical|high|medium|low|info",
      "category": "config_error|deprecation|performance",
      "line_hint": 42,
      "suggested_fix": "Konkreter Fix",
      "confidence": 0.9
    }
  ]
}
"""

_ANALYZE_COMPONENT_SYSTEM = """Du bist ein Home Assistant Custom Component Experte.

Analysiere Python-Code und manifest.json auf:
1. Fehlende async_setup_entry / async_unload_entry
2. Blocking I/O im async-Kontext (requests statt aiohttp/httpx)
3. Fehlende Typisierung
4. Fehlerhafte DOMAIN-Konstante
5. Fehlende Config Flow (config_flow: true in manifest ohne config_flow.py)
6. Deprecated HA-APIs (hass.data direkt statt entry.runtime_data)
7. Fehlende unique_id
8. Unsichere Patterns (eval, exec, subprocess ohne validate)

Antworte NUR mit gültigem JSON mit 'findings' Liste.
"""


class ConfigAnalysisAgent(BaseAgent):
    """
    Analysiert alle HA-Konfigurationsdateien auf Fehler, Deprecations und Best-Practice-Verletzungen.
    """

    agent_type = AgentType.CONFIG_ANALYSIS

    def _build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(AgentState)

        graph.add_node("discover_files", self._node_discover_files)
        graph.add_node("lint_yaml", self._node_lint_yaml)
        graph.add_node("check_ha_config", self._node_check_ha_config)
        graph.add_node("analyze_automations", self._node_analyze_automations)
        graph.add_node("analyze_templates", self._node_analyze_templates)
        graph.add_node("analyze_custom_components", self._node_analyze_custom_components)
        graph.add_node("check_deprecations", self._node_check_deprecations)
        graph.add_node("generate_findings", self._node_generate_findings)
        graph.add_node("done", self._node_done)

        graph.add_edge(START, "discover_files")
        graph.add_edge("discover_files", "lint_yaml")
        graph.add_edge("lint_yaml", "check_ha_config")
        graph.add_edge("check_ha_config", "analyze_automations")
        graph.add_edge("analyze_automations", "analyze_templates")
        graph.add_edge("analyze_templates", "analyze_custom_components")
        graph.add_edge("analyze_custom_components", "check_deprecations")
        graph.add_edge("check_deprecations", "generate_findings")
        graph.add_edge("generate_findings", "done")
        graph.add_edge("done", END)

        return graph.compile(checkpointer=MemorySaver())

    # -------------------------------------------------------------------------
    # Graph-Knoten
    # -------------------------------------------------------------------------

    async def _node_discover_files(self, state: AgentState) -> dict[str, Any]:
        """Ermittelt alle relevanten Konfigurationsdateien."""
        config_path = state.context.get("ha_config_path", "/config")
        result = await list_ha_files(config_path)

        if not result["success"]:
            logger.warning("Datei-Discovery fehlgeschlagen: %s", result.get("error"))
            return {"errors": [*state.errors, result.get("error", "Discovery error")]}

        categories = result.get("categories", {})
        logger.info(
            "Dateien entdeckt: %d total in %s",
            result.get("total_files", 0),
            config_path,
        )
        return {
            "context": {
                **state.context,
                "discovered_files": categories,
                "config_path": config_path,
            }
        }

    async def _node_lint_yaml(self, state: AgentState) -> dict[str, Any]:
        """Führt yamllint auf allen YAML-Dateien aus."""
        categories = state.context.get("discovered_files", {})
        all_yaml_files: list[str] = []
        for files in categories.values():
            all_yaml_files.extend(f for f in files if f.endswith(".yaml"))

        lint_issues: list[dict[str, Any]] = []
        for file_path in all_yaml_files[:50]:  # Maximal 50 Dateien pro Lauf
            result = await yamllint_check(file_path)
            if result.get("success") and result.get("issues"):
                for issue in result["issues"]:
                    issue["file"] = file_path
                    lint_issues.append(issue)

        logger.info("YAML-Lint: %d Issues in %d Dateien", len(lint_issues), len(all_yaml_files))

        new_findings: list[Finding] = list(state.findings)
        for issue in lint_issues:
            if issue.get("level") == "error":
                new_findings.append(
                    Finding(
                        title=f"YAML-Syntaxfehler: {Path(issue['file']).name}",
                        description=(
                            f"{issue['message']} "
                            f"(Zeile {issue.get('line', '?')}, Spalte {issue.get('col', '?')})"
                        ),
                        severity=FindingSeverity.HIGH,
                        category=FindingCategory.CONFIG_ERROR,
                        source_agent="config_analysis_agent",
                        confidence=0.99,
                        affected_files=[issue["file"]],
                    )
                )

        return {
            "findings": new_findings,
            "context": {**state.context, "lint_issues": lint_issues},
        }

    async def _node_check_ha_config(self, state: AgentState) -> dict[str, Any]:
        """Führt den offiziellen HA Config-Check aus."""
        config_path = state.context.get("config_path", "/config")
        result = await ha_config_check(config_path)

        new_findings: list[Finding] = list(state.findings)
        if result.get("success"):
            for issue in result.get("issues", []):
                severity = (
                    FindingSeverity.CRITICAL
                    if issue.get("level") == "ERROR"
                    else FindingSeverity.MEDIUM
                )
                new_findings.append(
                    Finding(
                        title=f"HA Config Check: {issue['message'][:60]}",
                        description=issue["message"],
                        severity=severity,
                        category=FindingCategory.CONFIG_ERROR,
                        source_agent="config_analysis_agent",
                        confidence=0.95,
                    )
                )
        else:
            logger.info("HA Config Check nicht verfügbar: %s", result.get("error"))

        return {"findings": new_findings}

    async def _node_analyze_automations(self, state: AgentState) -> dict[str, Any]:
        """LLM analysiert Automatisierungs-YAML auf Logikfehler."""
        automation_files = state.context.get("discovered_files", {}).get("automations", [])
        if not automation_files:
            return {"iteration": state.iteration}

        new_findings: list[Finding] = list(state.findings)

        # Maximal 3 Automatisierungs-Dateien pro Lauf analysieren
        for file_path in automation_files[:3]:
            file_result = await read_file(file_path, max_lines=300)
            if not file_result.get("success"):
                continue

            content = file_result["content"]
            prompt = (
                f"Analysiere diese Home Assistant Automatisierungs-Datei:\n"
                f"Datei: {file_path}\n\n"
                f"```yaml\n{content}\n```"
            )
            raw = await self._call_llm(state, prompt, system_prompt=_ANALYZE_AUTOMATION_SYSTEM)
            findings = self._parse_llm_findings_with_file(raw, "config_analysis_agent", file_path)
            new_findings.extend(findings)

        return {"findings": new_findings}

    async def _node_analyze_templates(self, state: AgentState) -> dict[str, Any]:
        """Prüft Jinja2-Templates in YAML-Dateien auf Syntaxfehler."""
        config_path = state.context.get("config_path", "/config")

        # Alle Templates (value_template, trigger templates usw.) finden
        grep_result = await grep_pattern(
            pattern=r"_template:\s*.+",
            directory=config_path,
            file_glob="**/*.yaml",
            max_results=100,
        )

        new_findings: list[Finding] = list(state.findings)
        template_pattern = re.compile(r"_template:\s*['\"]?(.+?)['\"]?\s*$")

        for match in grep_result.get("matches", []):
            line = match.get("line_content", "")
            m = template_pattern.search(line)
            if not m:
                continue

            template_str = m.group(1).strip()
            # Nur prüfen wenn Template-Syntax vorhanden ({{...}} oder {%...%})
            if "{{" not in template_str and "{%" not in template_str:
                continue

            check = await jinja2_syntax_check(template_str)
            if not check.get("valid", True) and check.get("error"):
                new_findings.append(
                    Finding(
                        title=f"Jinja2-Template-Fehler in {Path(match['file']).name}",
                        description=(
                            f"Ungültiges Template in Zeile {match['line_number']}: "
                            f"{check['error']}\n"
                            f"Template: {template_str[:100]}"
                        ),
                        severity=FindingSeverity.HIGH,
                        category=FindingCategory.CONFIG_ERROR,
                        source_agent="config_analysis_agent",
                        confidence=0.95,
                        affected_files=[match["file"]],
                        metadata={"line": match["line_number"], "template": template_str},
                    )
                )

        return {"findings": new_findings}

    async def _node_analyze_custom_components(self, state: AgentState) -> dict[str, Any]:
        """Analysiert Custom Components auf HA-Best-Practices."""
        component_files = state.context.get("discovered_files", {}).get("custom_components", [])
        python_files = [f for f in component_files if f.endswith(".py")]
        if not python_files:
            return {"iteration": state.iteration}

        new_findings: list[Finding] = list(state.findings)

        # Syntaxprüfung aller Python-Dateien
        for file_path in python_files[:20]:
            result = await python_syntax_check(file_path)
            for issue in result.get("issues", []):
                new_findings.append(
                    Finding(
                        title=f"Python-Syntaxfehler: {Path(file_path).name}",
                        description=f"{issue['message']} (Zeile {issue.get('line', '?')})",
                        severity=FindingSeverity.CRITICAL,
                        category=FindingCategory.CONFIG_ERROR,
                        source_agent="config_analysis_agent",
                        confidence=0.99,
                        affected_files=[file_path],
                    )
                )

        # LLM-Analyse der __init__.py Dateien
        init_files = [f for f in python_files if f.endswith("__init__.py")][:3]
        for file_path in init_files:
            file_result = await read_file(file_path, max_lines=250)
            if not file_result.get("success"):
                continue

            prompt = (
                f"Analysiere diesen Home Assistant Custom Component Code:\n"
                f"Datei: {file_path}\n\n"
                f"```python\n{file_result['content']}\n```"
            )
            raw = await self._call_llm(state, prompt, system_prompt=_ANALYZE_COMPONENT_SYSTEM)
            findings = self._parse_llm_findings_with_file(raw, "config_analysis_agent", file_path)
            new_findings.extend(findings)

        return {"findings": new_findings}

    async def _node_check_deprecations(self, state: AgentState) -> dict[str, Any]:
        """Sucht nach bekannten Deprecation-Patterns in YAML und ESPHome."""
        config_path = state.context.get("config_path", "/config")
        new_findings: list[Finding] = list(state.findings)

        # HA-Deprecations in YAML
        for pattern, title, fix in _DEPRECATED_PATTERNS:
            result = await grep_pattern(
                pattern=pattern.pattern,
                directory=config_path,
                file_glob="**/*.yaml",
                max_results=10,
            )
            if result.get("match_count", 0) > 0:
                affected = [m["file"] for m in result["matches"]]
                new_findings.append(
                    Finding(
                        title=f"Deprecated: {title}",
                        description=(
                            f"Veraltetes Pattern gefunden in {result['match_count']} Stelle(n). "
                            f"Fix: {fix}"
                        ),
                        severity=FindingSeverity.MEDIUM,
                        category=FindingCategory.DEPRECATION,
                        source_agent="config_analysis_agent",
                        confidence=0.9,
                        affected_files=list(dict.fromkeys(affected)),
                        suggested_fix=fix,
                    )
                )

        # ESPHome-Deprecations
        esphome_files = state.context.get("discovered_files", {}).get("esphome", [])
        if esphome_files:
            esphome_dir = str(Path(esphome_files[0]).parent) if esphome_files else config_path
            for pattern, title, fix in _ESPHOME_DEPRECATED:
                result = await grep_pattern(
                    pattern=pattern.pattern,
                    directory=esphome_dir,
                    file_glob="**/*.yaml",
                    max_results=5,
                )
                if result.get("match_count", 0) > 0:
                    affected = [m["file"] for m in result["matches"]]
                    new_findings.append(
                        Finding(
                            title=f"ESPHome Deprecated: {title}",
                            description=f"Fix erforderlich: {fix}",
                            severity=FindingSeverity.HIGH,
                            category=FindingCategory.DEPRECATION,
                            source_agent="config_analysis_agent",
                            confidence=0.92,
                            affected_files=list(dict.fromkeys(affected)),
                            suggested_fix=fix,
                        )
                    )

        return {"findings": new_findings}

    async def _node_generate_findings(self, state: AgentState) -> dict[str, Any]:
        """Dedupliziert, berechnet Risk-Scores und priorisiert Findings."""
        findings = _deduplicate_findings(state.findings)
        findings = _calculate_risk_scores(findings)
        findings.sort(key=lambda f: (f.risk_score, f.severity), reverse=True)

        logger.info(
            "Config-Analyse abgeschlossen: %d Findings", len(findings)
        )
        return {"findings": findings}

    async def _node_done(self, state: AgentState) -> dict[str, Any]:
        return {"status": AgentStatus.IDLE}

    # -------------------------------------------------------------------------
    # Hilfsmethoden
    # -------------------------------------------------------------------------

    def _parse_llm_findings_with_file(
        self, raw: str, source_agent: str, file_path: str
    ) -> list[Finding]:
        """Parst LLM-Findings und setzt affected_files."""
        try:
            data = _extract_json(raw)
            items = data.get("findings", [])
        except (json.JSONDecodeError, ValueError):
            return []

        findings: list[Finding] = []
        for item in items:
            try:
                severity = FindingSeverity(item.get("severity", "medium"))
                category = FindingCategory(item.get("category", "config_error"))
            except ValueError:
                severity = FindingSeverity.MEDIUM
                category = FindingCategory.CONFIG_ERROR

            findings.append(
                Finding(
                    title=item.get("title", "Config-Problem"),
                    description=item.get("description", ""),
                    severity=severity,
                    category=category,
                    source_agent=source_agent,
                    confidence=float(item.get("confidence", 0.75)),
                    affected_files=[file_path],
                    suggested_fix=item.get("suggested_fix"),
                    metadata={"line_hint": item.get("line_hint")},
                )
            )
        return findings
