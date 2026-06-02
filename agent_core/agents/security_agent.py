"""
Security-Agent — vollständige Implementierung.

Workflow:
  discover_scope → static_scan → cve_check → llm_deep_analysis
                → score_risks → generate_report → done
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_core.tools.analysis_tools import list_ha_files, read_file
from models.agent_models import AgentState
from models.enums import AgentStatus, AgentType, FindingSeverity, SecurityCheckType
from models.security_models import CVEEntry, SecurityIssue, SecurityReport
from security.scanner import lookup_cve_osv, scan_directory

from .base_agent import BaseAgent
from .log_analysis_agent import _extract_json

logger = logging.getLogger(__name__)

_DEEP_ANALYSIS_SYSTEM = """Du bist ein Home Assistant Security Expert und Penetration Tester.

Analysiere den gegebenen Konfigurationsausschnitt auf Sicherheitsrisiken die
statische Regex-Scanner übersehen könnten:

1. Business-Logic-Fehler in Automationen (unbeabsichtigte Aktionen)
2. Privilege-Escalation über HA-Services
3. Unsichere Integrationen (HACS, Custom Components ohne Reviews)
4. Fehlende Authentifizierung in Webhook-Triggern
5. Unverschlüsselte Kommunikation (HTTP statt HTTPS)
6. Fehlende Rate-Limiting
7. Insecure Default-Konfigurationen

Antworte NUR mit gültigem JSON:
{
  "security_issues": [
    {
      "check_type": "missing_auth|privilege_escalation|insecure_component|supply_chain",
      "title": "Kurzer Titel",
      "description": "Detaillierte Beschreibung",
      "severity": "critical|high|medium|low|info",
      "cvss_score": 7.5,
      "remediation": "Konkreter Fix",
      "confidence": 0.85
    }
  ]
}
"""


class SecurityAgent(BaseAgent):
    """
    Führt umfassende Security-Audits auf HA-Konfigurationen durch.

    Kombiniert deterministischen statischen Scan (Regex/AST) mit
    LLM-basierter Tiefenanalyse und CVE-Lookup via OSV.dev.
    """

    agent_type = AgentType.SECURITY

    def _build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(AgentState)

        graph.add_node("discover_scope", self._node_discover_scope)
        graph.add_node("static_scan", self._node_static_scan)
        graph.add_node("cve_check", self._node_cve_check)
        graph.add_node("llm_deep_analysis", self._node_llm_deep_analysis)
        graph.add_node("score_risks", self._node_score_risks)
        graph.add_node("generate_report", self._node_generate_report)
        graph.add_node("done", self._node_done)

        graph.add_edge(START, "discover_scope")
        graph.add_edge("discover_scope", "static_scan")
        graph.add_edge("static_scan", "cve_check")
        graph.add_edge("cve_check", "llm_deep_analysis")
        graph.add_edge("llm_deep_analysis", "score_risks")
        graph.add_edge("score_risks", "generate_report")
        graph.add_edge("generate_report", "done")
        graph.add_edge("done", END)

        return graph.compile(checkpointer=MemorySaver())

    async def _node_discover_scope(self, state: AgentState) -> dict[str, Any]:
        """Ermittelt den Scan-Umfang."""
        config_path = state.context.get("ha_config_path", "/config")
        result = await list_ha_files(config_path)
        scanned: list[str] = []
        if result.get("success"):
            for files in result.get("categories", {}).values():
                scanned.extend(files)
        logger.info("Security-Scan Scope: %d Dateien in %s", len(scanned), config_path)
        return {
            "context": {
                **state.context,
                "config_path": config_path,
                "scanned_files": scanned,
            }
        }

    async def _node_static_scan(self, state: AgentState) -> dict[str, Any]:
        """Führt den deterministischen statischen Security-Scan durch."""
        config_path = state.context.get("config_path", "/config")
        issues = await scan_directory(config_path)
        logger.info(
            "Statischer Scan: %d Security-Issues gefunden (%d critical)",
            len(issues),
            sum(1 for i in issues if i.severity == FindingSeverity.CRITICAL),
        )
        return {
            "security_issues": issues,
            "context": {**state.context, "static_scan_count": len(issues)},
        }

    async def _node_cve_check(self, state: AgentState) -> dict[str, Any]:
        """Prüft Custom-Component-Abhängigkeiten auf bekannte CVEs via OSV.dev."""
        import glob

        config_path = state.context.get("config_path", "/config")
        manifest_files = glob.glob(f"{config_path}/**/manifest.json", recursive=True)

        cve_matches: list[CVEEntry] = []
        checked_packages: set[str] = set()

        for mf_path in manifest_files[:10]:
            try:
                import json as json_mod
                from pathlib import Path

                manifest = json_mod.loads(Path(mf_path).read_text())
                requirements: list[str] = manifest.get("requirements", [])
            except Exception:
                continue

            for req in requirements:
                # Paketname extrahieren (ohne Versionsconstraints)
                pkg = req.split(">=")[0].split("==")[0].split("<=")[0].split(">")[0].split("<")[0].strip()
                if pkg in checked_packages:
                    continue
                checked_packages.add(pkg)

                vulns = await lookup_cve_osv(pkg)
                for vuln in vulns[:3]:  # Max 3 CVEs pro Paket
                    severity_map = {"CRITICAL": FindingSeverity.CRITICAL, "HIGH": FindingSeverity.HIGH,
                                    "MEDIUM": FindingSeverity.MEDIUM, "LOW": FindingSeverity.LOW}
                    db_severity = vuln.get("database_specific", {}).get("severity", "MEDIUM")
                    cve_matches.append(
                        CVEEntry(
                            cve_id=vuln.get("id", "UNKNOWN"),
                            description=vuln.get("summary", ""),
                            severity=severity_map.get(db_severity, FindingSeverity.MEDIUM),
                            affected_packages=[pkg],
                            references=[a.get("url", "") for a in vuln.get("references", [])[:3]],
                        )
                    )

        if cve_matches:
            logger.warning("CVE-Matches: %d Schwachstellen in Abhängigkeiten gefunden", len(cve_matches))

        return {
            "context": {
                **state.context,
                "cve_matches": [c.model_dump(mode="json") for c in cve_matches],
            }
        }

    async def _node_llm_deep_analysis(self, state: AgentState) -> dict[str, Any]:
        """LLM-basierte Tiefenanalyse für Business-Logic und komplexe Sicherheitsrisiken."""
        config_path = state.context.get("config_path", "/config")

        # Repräsentativen Ausschnitt für LLM-Analyse laden
        sample_content = await self._build_analysis_sample(config_path)
        if not sample_content:
            return {"iteration": state.iteration}

        prompt = (
            f"Analysiere diese Home Assistant Konfiguration auf Sicherheitsrisiken:\n\n"
            f"{sample_content}\n\n"
            f"Bereits gefundene Issues (nicht wiederholen): "
            f"{len(state.security_issues)} statisch erkannte Probleme."
        )
        raw = await self._call_llm(state, prompt, system_prompt=_DEEP_ANALYSIS_SYSTEM)

        new_issues = self._parse_llm_security_issues(raw)
        combined = list(state.security_issues) + new_issues
        logger.info("LLM-Tiefenanalyse: %d zusätzliche Issues", len(new_issues))
        return {"security_issues": combined}

    async def _node_score_risks(self, state: AgentState) -> dict[str, Any]:
        """Berechnet finale Risk-Scores und entfernt Duplikate."""
        issues = _deduplicate_security_issues(state.security_issues)
        issues.sort(key=lambda i: i.risk_score, reverse=True)
        return {"security_issues": issues}

    async def _node_generate_report(self, state: AgentState) -> dict[str, Any]:
        """Erstellt den SecurityReport mit Gesamtbewertung."""
        issues = state.security_issues
        cve_data = state.context.get("cve_matches", [])
        cve_entries = [CVEEntry(**c) for c in cve_data]

        severity_counts = {s: 0 for s in FindingSeverity}
        for issue in issues:
            try:
                severity_counts[FindingSeverity(issue.severity)] += 1
            except ValueError:
                pass

        overall_score = 0.0
        if issues:
            overall_score = min(
                sum(i.risk_score for i in issues[:10]) / min(len(issues), 10),
                10.0,
            )

        report = SecurityReport(
            issues=issues,
            cve_matches=cve_entries,
            overall_risk_score=round(overall_score, 2),
            critical_count=severity_counts.get(FindingSeverity.CRITICAL, 0),
            high_count=severity_counts.get(FindingSeverity.HIGH, 0),
            medium_count=severity_counts.get(FindingSeverity.MEDIUM, 0),
            low_count=severity_counts.get(FindingSeverity.LOW, 0),
            scanned_files=state.context.get("scanned_files", []),
            summary=self._build_summary(issues, cve_entries),
            recommendations=self._build_recommendations(issues),
        )

        logger.info(
            "Security-Report: Risk-Score=%.1f | critical=%d high=%d medium=%d low=%d",
            report.overall_risk_score,
            report.critical_count,
            report.high_count,
            report.medium_count,
            report.low_count,
        )

        # Immer in den persistenten Store schreiben — auch beim Full Audit
        try:
            from security.store import set_latest_issues
            set_latest_issues(issues)
        except Exception as _exc:
            logger.warning("Security-Store: Issues konnten nicht gespeichert werden: %s", _exc)

        return {
            "context": {
                **state.context,
                "security_report": report.model_dump(mode="json"),
            }
        }

    async def _node_done(self, state: AgentState) -> dict[str, Any]:
        return {"status": AgentStatus.IDLE}

    # -------------------------------------------------------------------------
    # Hilfsmethoden
    # -------------------------------------------------------------------------

    async def _build_analysis_sample(self, config_path: str) -> str:
        """Baut einen repräsentativen Konfigurationsausschnitt für LLM-Analyse."""
        import glob

        sample_parts: list[str] = []
        priority_files = (
            glob.glob(f"{config_path}/configuration.yaml")
            + glob.glob(f"{config_path}/automations.yaml")
            + glob.glob(f"{config_path}/custom_components/*/__init__.py")[:2]
        )

        for fp in priority_files[:4]:
            result = await read_file(fp, max_lines=100)
            if result.get("success"):
                sample_parts.append(f"### {fp}\n{result['content']}")

        return "\n\n".join(sample_parts)[:6000]  # Kontextlimit schonen

    def _parse_llm_security_issues(self, raw: str) -> list[SecurityIssue]:
        """Parst LLM-generierte Security-Issues aus JSON-Antwort."""
        try:
            data = _extract_json(raw)
            items = data.get("security_issues", [])
        except (json.JSONDecodeError, ValueError):
            return []

        issues: list[SecurityIssue] = []
        for item in items:
            try:
                check_type = SecurityCheckType(item.get("check_type", "insecure_component"))
            except ValueError:
                check_type = SecurityCheckType.INSECURE_COMPONENT
            try:
                severity = FindingSeverity(item.get("severity", "medium"))
            except ValueError:
                severity = FindingSeverity.MEDIUM

            from security.scanner import _severity_to_score
            issues.append(
                SecurityIssue(
                    check_type=check_type,
                    title=item.get("title", "Security-Problem"),
                    description=item.get("description", ""),
                    severity=severity,
                    cvss_score=item.get("cvss_score"),
                    risk_score=item.get("cvss_score") or _severity_to_score(severity),
                    remediation=item.get("remediation"),
                )
            )
        return issues

    def _build_summary(self, issues: list[SecurityIssue], cves: list[CVEEntry]) -> str:
        critical = sum(1 for i in issues if i.severity == FindingSeverity.CRITICAL)
        if critical > 0:
            return (
                f"KRITISCH: {critical} kritische Sicherheitsprobleme gefunden. "
                f"Sofortiger Handlungsbedarf. {len(cves)} CVE-Treffer in Abhängigkeiten."
            )
        high = sum(1 for i in issues if i.severity == FindingSeverity.HIGH)
        if high > 0:
            return f"{high} Sicherheitsprobleme mit hohem Risiko. Baldiger Handlungsbedarf."
        return f"Keine kritischen Probleme. {len(issues)} Hinweise zur Verbesserung."

    def _build_recommendations(self, issues: list[SecurityIssue]) -> list[str]:
        recs: list[str] = []
        has_secrets = any(i.check_type == SecurityCheckType.SECRETS_DETECTION for i in issues)
        has_docker = any(i.check_type == SecurityCheckType.DOCKER_SECURITY for i in issues)
        if has_secrets:
            recs.append("Alle Credentials in HA !secret oder Umgebungsvariablen auslagern")
        if has_docker:
            recs.append("Docker-Images pinnen (keine :latest-Tags), non-root User verwenden")
        recs.append("Regelmäßige Security-Audits einplanen (mindestens monatlich)")
        recs.append("HA-Updates zeitnah einspielen (Sicherheitspatches)")
        return recs


def _deduplicate_security_issues(issues: list[SecurityIssue]) -> list[SecurityIssue]:
    """Entfernt doppelte Security-Issues anhand von Titel + Dateipfad."""
    seen: set[str] = set()
    unique: list[SecurityIssue] = []
    for issue in issues:
        key = f"{issue.title[:50]}:{issue.file_path}:{issue.line_number}"
        if key not in seen:
            seen.add(key)
            unique.append(issue)
    return unique
