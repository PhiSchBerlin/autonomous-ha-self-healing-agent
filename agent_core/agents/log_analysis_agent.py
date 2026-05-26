"""
Log-Analyse-Agent — vollständige Implementierung.

Workflow:
  ingest_logs → classify → correlate → pattern_detection
             → generate_findings → [human_review?] → done

Verwendet echte Tool-Calls (yamllint, grep, Parser) und strukturierte
LLM-Outputs (JSON-Schema-enforced).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal
from uuid import uuid4

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from agent_core.tools.log_parser import detect_patterns, parse_log
from models.agent_models import AgentState
from models.enums import AgentMode, AgentStatus, AgentType, FindingCategory, FindingSeverity, LogSource
from models.log_models import Finding, LogEntry

from .base_agent import BaseAgent

logger = logging.getLogger(__name__)

_CLASSIFY_SYSTEM = """Du bist ein erfahrener Home Assistant Site Reliability Engineer.

Analysiere die Log-Einträge und identifiziere Probleme.

Antworte NUR mit gültigem JSON in dieser Struktur:
{
  "findings": [
    {
      "title": "Kurzer Titel",
      "description": "Detaillierte Beschreibung",
      "severity": "critical|high|medium|low|info",
      "category": "log_error|config_error|security|performance|deprecation|regression",
      "root_cause": "Vermutete Ursache",
      "suggested_fix": "Konkreter Lösungsvorschlag",
      "confidence": 0.85,
      "affected_integration": "mqtt|zigbee2mqtt|esphome|etc oder null"
    }
  ],
  "summary": "Kurze Gesamtbewertung in 2-3 Sätzen"
}

Wichtig:
- Nur echte Probleme, keine Infos/Debug-Nachrichten
- Root Cause so präzise wie möglich
- Suggested Fix muss umsetzbar sein
"""

_CORRELATE_SYSTEM = """Du bist ein Home Assistant Experte für Root Cause Analysis.

Analysiere ob die gegebenen Findings zusammenhängen.
Suche nach: gemeinsamen Integrationen, Kausalketten, Timing-Korrelationen.

Antworte NUR mit gültigem JSON:
{
  "correlations": [
    {
      "finding_titles": ["Titel A", "Titel B"],
      "relationship": "Beschreibung der Beziehung",
      "root_finding": "Titel des Kern-Problems"
    }
  ],
  "root_cause_chain": "Beschreibung der Ursachenkette (wenn erkennbar)"
}
"""


class LogAnalysisAgent(BaseAgent):
    """
    Vollständiger Log-Analyse-Agent mit LangGraph-Workflow.

    Verarbeitet Logs aller HA-Quellen, erkennt Patterns, korreliert Probleme
    und erzeugt priorisierte Findings mit Root-Cause-Analyse.
    """

    agent_type = AgentType.LOG_ANALYSIS

    def _build_graph(self) -> CompiledStateGraph:
        graph = StateGraph(AgentState)

        graph.add_node("ingest_logs", self._node_ingest_logs)
        graph.add_node("classify", self._node_classify)
        graph.add_node("correlate", self._node_correlate)
        graph.add_node("pattern_detection", self._node_pattern_detection)
        graph.add_node("generate_findings", self._node_generate_findings)
        graph.add_node("human_review", self._node_human_review)
        graph.add_node("done", self._node_done)

        graph.add_edge(START, "ingest_logs")
        graph.add_edge("ingest_logs", "classify")
        graph.add_edge("classify", "correlate")
        graph.add_edge("correlate", "pattern_detection")
        graph.add_edge("pattern_detection", "generate_findings")
        graph.add_conditional_edges(
            "generate_findings",
            self._route_after_findings,
            {"human_review": "human_review", "done": "done"},
        )
        graph.add_edge("human_review", "done")
        graph.add_edge("done", END)

        checkpointer = MemorySaver()
        return graph.compile(checkpointer=checkpointer)

    def _route_after_findings(
        self, state: AgentState
    ) -> Literal["human_review", "done"]:
        """Leitet zu Human Review weiter wenn kritische Findings und APPROVAL-Modus."""
        has_critical = any(
            f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH)
            for f in state.findings
        )
        if state.mode == AgentMode.APPROVAL and has_critical:
            return "human_review"
        return "done"

    # -------------------------------------------------------------------------
    # Graph-Knoten
    # -------------------------------------------------------------------------

    async def _node_ingest_logs(self, state: AgentState) -> dict[str, Any]:
        """Normalisiert Rohlogs aus dem Kontext zu LogEntry-Objekten."""
        raw_logs: list[dict[str, Any]] = state.context.get("raw_logs", [])
        parsed: list[LogEntry] = []

        for raw in raw_logs:
            source_str = raw.get("source", "ha_core")
            try:
                source = LogSource(source_str)
            except ValueError:
                source = LogSource.HA_CORE

            text = raw.get("text", raw.get("content", ""))
            if not text:
                continue

            entries = parse_log(text, source)
            parsed.extend(entries)

        logger.info("Log-Ingest: %d Einträge aus %d Quellen", len(parsed), len(raw_logs))

        return {
            "log_entries": parsed,
            "context": {**state.context, "ingested_count": len(parsed)},
        }

    async def _node_classify(self, state: AgentState) -> dict[str, Any]:
        """LLM klassifiziert Log-Einträge und extrahiert erste Findings."""
        entries = state.log_entries
        if not entries:
            logger.info("Keine Log-Einträge zum Klassifizieren")
            return {}

        # Nur Fehler und Warnungen ans LLM schicken (Kontextlimit schonen)
        error_entries = [e for e in entries if e.level in ("ERROR", "CRITICAL", "WARNING")]
        sample = error_entries[:80] if len(error_entries) > 80 else error_entries

        log_text = "\n".join(
            f"[{e.level}] {e.source} | {e.component or 'unknown'} | {e.message}"
            + (f"\nTraceback:\n{e.traceback}" if e.traceback else "")
            for e in sample
        )

        prompt = (
            f"Analysiere diese {len(sample)} Home Assistant Log-Einträge "
            f"(von insgesamt {len(entries)} Einträgen, {len(error_entries)} Fehler/Warnungen):\n\n"
            f"{log_text}"
        )

        raw_response = await self._call_llm(state, prompt, system_prompt=_CLASSIFY_SYSTEM)
        llm_findings = self._parse_llm_findings(raw_response, "log_analysis_agent")

        return {"findings": llm_findings}

    async def _node_correlate(self, state: AgentState) -> dict[str, Any]:
        """Korreliert gefundene Probleme und erkennt Kausalketten."""
        if len(state.findings) < 2:
            return {}

        findings_text = "\n".join(
            f"- [{f.severity}] {f.title}: {f.description} "
            f"(Ursache: {f.root_cause or 'unbekannt'})"
            for f in state.findings[:30]
        )

        prompt = f"Analysiere Korrelationen zwischen diesen Findings:\n\n{findings_text}"
        raw_response = await self._call_llm(state, prompt, system_prompt=_CORRELATE_SYSTEM)

        # Korrelationsdaten in den Kontext speichern
        try:
            data = _extract_json(raw_response)
            correlations = data.get("correlations", [])
            root_cause_chain = data.get("root_cause_chain", "")
            return {
                "context": {
                    **state.context,
                    "correlations": correlations,
                    "root_cause_chain": root_cause_chain,
                }
            }
        except (json.JSONDecodeError, ValueError):
            return {}

    async def _node_pattern_detection(self, state: AgentState) -> dict[str, Any]:
        """Erkennt statistische Muster ohne LLM (schnell, deterministisch)."""
        patterns = detect_patterns(state.log_entries)
        logger.info("Pattern-Erkennung: %d Muster gefunden", len(patterns))

        # Patterns zu Findings konvertieren wenn signifikant
        new_findings: list[Finding] = list(state.findings)
        for pattern in patterns:
            if pattern.occurrences >= 5 and pattern.confidence >= 0.7:
                new_findings.append(
                    Finding(
                        title=f"Wiederholungsmuster: {pattern.pattern_type}",
                        description=pattern.description,
                        severity=FindingSeverity.MEDIUM,
                        category=FindingCategory.REGRESSION,
                        source_agent="log_analysis_agent",
                        confidence=pattern.confidence,
                        affected_log_entries=pattern.affected_entries,
                    )
                )

        return {
            "findings": new_findings,
            "context": {**state.context, "patterns_detected": len(patterns)},
        }

    async def _node_generate_findings(self, state: AgentState) -> dict[str, Any]:
        """Priorisiert und dedupliziert Findings, berechnet Risk-Scores."""
        findings = _deduplicate_findings(state.findings)
        findings = _calculate_risk_scores(findings)
        findings.sort(key=lambda f: (f.risk_score, f.severity), reverse=True)

        logger.info(
            "Findings finalisiert: %d total (%d critical/high)",
            len(findings),
            sum(1 for f in findings if f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH)),
        )
        return {"findings": findings}

    async def _node_human_review(self, state: AgentState) -> dict[str, Any]:
        """Markiert kritische Findings zur menschlichen Überprüfung."""
        critical = [
            f for f in state.findings
            if f.severity in (FindingSeverity.CRITICAL, FindingSeverity.HIGH)
        ]
        logger.warning(
            "Human Review erforderlich: %d kritische Findings warten auf Bestätigung",
            len(critical),
        )
        pending_ids = [f.id for f in critical]
        return {
            "pending_approval_ids": pending_ids,
            "status": AgentStatus.WAITING_APPROVAL,
        }

    async def _node_done(self, state: AgentState) -> dict[str, Any]:
        """Setzt den Status auf IDLE und loggt die Zusammenfassung."""
        logger.info(
            "Log-Analyse abgeschlossen: %d Findings, %d Log-Einträge verarbeitet",
            len(state.findings),
            len(state.log_entries),
        )
        return {"status": AgentStatus.IDLE}

    # -------------------------------------------------------------------------
    # Hilfsmethoden
    # -------------------------------------------------------------------------

    def _parse_llm_findings(self, raw: str, source_agent: str) -> list[Finding]:
        """Extrahiert Finding-Objekte aus einer LLM-JSON-Antwort."""
        try:
            data = _extract_json(raw)
            findings_data = data.get("findings", [])
        except (json.JSONDecodeError, ValueError):
            logger.warning("LLM-Antwort konnte nicht als JSON geparst werden")
            return []

        findings: list[Finding] = []
        for item in findings_data:
            try:
                severity = FindingSeverity(item.get("severity", "medium"))
                category = FindingCategory(item.get("category", "log_error"))
            except ValueError:
                severity = FindingSeverity.MEDIUM
                category = FindingCategory.LOG_ERROR

            findings.append(
                Finding(
                    title=item.get("title", "Unbekanntes Problem"),
                    description=item.get("description", ""),
                    severity=severity,
                    category=category,
                    source_agent=source_agent,
                    confidence=float(item.get("confidence", 0.7)),
                    root_cause=item.get("root_cause"),
                    suggested_fix=item.get("suggested_fix"),
                    metadata={
                        "affected_integration": item.get("affected_integration"),
                    },
                )
            )

        return findings


# ---------------------------------------------------------------------------
# Modul-Hilfsfunktionen (nicht Teil der Klasse)
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> dict[str, Any]:
    """Extrahiert JSON aus einer LLM-Antwort, auch wenn Text darum herum ist."""
    # Direkter Parse-Versuch
    stripped = text.strip()
    if stripped.startswith("{"):
        return json.loads(stripped)

    # JSON-Block in Markdown suchen
    import re as _re
    fence_match = _re.search(r"```(?:json)?\s*(\{.+?\})\s*```", stripped, _re.S)
    if fence_match:
        return json.loads(fence_match.group(1))

    # Erstes { ... } finden
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        return json.loads(stripped[start:end + 1])

    raise ValueError("Kein JSON in LLM-Antwort gefunden")


def _deduplicate_findings(findings: list[Finding]) -> list[Finding]:
    """Entfernt doppelte Findings anhand von Titel-Ähnlichkeit."""
    seen_titles: set[str] = set()
    unique: list[Finding] = []
    for f in findings:
        key = f.title.lower().strip()[:60]
        if key not in seen_titles:
            seen_titles.add(key)
            unique.append(f)
    return unique


def _calculate_risk_scores(findings: list[Finding]) -> list[Finding]:
    """Berechnet Risk-Scores basierend auf Severity und Confidence."""
    severity_weights = {
        FindingSeverity.CRITICAL: 9.0,
        FindingSeverity.HIGH: 7.0,
        FindingSeverity.MEDIUM: 4.5,
        FindingSeverity.LOW: 2.0,
        FindingSeverity.INFO: 0.5,
    }
    for f in findings:
        base = severity_weights.get(FindingSeverity(f.severity), 4.5)
        f.risk_score = round(min(base * f.confidence, 10.0), 2)
    return findings
