"""Unit-Tests für Log-Analyse- und Config-Analyse-Agenten mit Mock-LLM."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agent_core.agents.log_analysis_agent import (
    LogAnalysisAgent,
    _calculate_risk_scores,
    _deduplicate_findings,
    _extract_json,
)
from llm_backends.base import LLMBackendConfig, LLMResponse
from models.agent_models import AgentRunConfig, AgentState
from models.enums import AgentMode, AgentType, FindingCategory, FindingSeverity, LogSource
from models.log_models import Finding, LogEntry


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_response() -> LLMResponse:
    return LLMResponse(
        content="""
{
  "findings": [
    {
      "title": "MQTT-Verbindungsfehler",
      "description": "Broker nicht erreichbar",
      "severity": "high",
      "category": "log_error",
      "root_cause": "Broker läuft nicht",
      "suggested_fix": "Broker-Dienst prüfen",
      "confidence": 0.9,
      "affected_integration": "mqtt"
    }
  ],
  "summary": "Ein kritisches MQTT-Problem erkannt."
}
""",
        model="test-model",
        backend="ollama",  # type: ignore[arg-type]
        total_tokens=100,
    )


@pytest.fixture
def mock_llm(mock_llm_response: LLMResponse) -> MagicMock:
    llm = MagicMock()
    llm.complete = AsyncMock(return_value=mock_llm_response)
    llm.config = LLMBackendConfig(backend_type="ollama", model="test")  # type: ignore[arg-type]
    return llm


@pytest.fixture
def agent_config() -> AgentRunConfig:
    return AgentRunConfig(
        agent_type=AgentType.LOG_ANALYSIS,
        mode=AgentMode.ADVISORY,
        dry_run=True,
    )


@pytest.fixture
def sample_log_entries() -> list[LogEntry]:
    from datetime import UTC, datetime
    return [
        LogEntry(
            source=LogSource.HA_CORE,
            level="ERROR",
            message="Cannot connect to MQTT broker",
            raw="2024-11-15 10:00:00.000 ERROR (MainThread) [homeassistant.components.mqtt] Cannot connect",
            timestamp=datetime.now(UTC),
            component="homeassistant.components.mqtt",
            integration="mqtt",
        ),
        LogEntry(
            source=LogSource.HA_CORE,
            level="WARNING",
            message="Integration zigbee2mqtt is not loaded",
            raw="2024-11-15 10:00:01.000 WARNING (MainThread) [homeassistant.loader] ...",
            timestamp=datetime.now(UTC),
        ),
    ]


# ---------------------------------------------------------------------------
# Tests: _extract_json
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_parses_plain_json(self) -> None:
        result = _extract_json('{"key": "value"}')
        assert result == {"key": "value"}

    def test_parses_json_in_markdown_fence(self) -> None:
        text = '```json\n{"key": "value"}\n```'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_extracts_json_from_prose(self) -> None:
        text = 'Here is my answer: {"key": "value"} thank you.'
        result = _extract_json(text)
        assert result == {"key": "value"}

    def test_raises_on_no_json(self) -> None:
        with pytest.raises(ValueError):
            _extract_json("just plain text no json here")


# ---------------------------------------------------------------------------
# Tests: _deduplicate_findings
# ---------------------------------------------------------------------------

class TestDeduplicateFindings:
    def _make_finding(self, title: str) -> Finding:
        return Finding(
            title=title,
            description="test",
            severity=FindingSeverity.MEDIUM,
            category=FindingCategory.LOG_ERROR,
            source_agent="test",
            confidence=0.8,
        )

    def test_removes_duplicates(self) -> None:
        findings = [
            self._make_finding("MQTT Fehler"),
            self._make_finding("MQTT Fehler"),
            self._make_finding("Anderes Problem"),
        ]
        result = _deduplicate_findings(findings)
        assert len(result) == 2

    def test_preserves_unique_findings(self) -> None:
        findings = [self._make_finding(f"Problem {i}") for i in range(5)]
        result = _deduplicate_findings(findings)
        assert len(result) == 5

    def test_empty_list(self) -> None:
        assert _deduplicate_findings([]) == []


# ---------------------------------------------------------------------------
# Tests: _calculate_risk_scores
# ---------------------------------------------------------------------------

class TestCalculateRiskScores:
    def _make_finding(self, severity: FindingSeverity, confidence: float) -> Finding:
        return Finding(
            title="Test",
            description="test",
            severity=severity,
            category=FindingCategory.LOG_ERROR,
            source_agent="test",
            confidence=confidence,
        )

    def test_critical_gets_high_score(self) -> None:
        findings = [self._make_finding(FindingSeverity.CRITICAL, 1.0)]
        result = _calculate_risk_scores(findings)
        assert result[0].risk_score >= 9.0

    def test_low_severity_gets_low_score(self) -> None:
        findings = [self._make_finding(FindingSeverity.LOW, 0.5)]
        result = _calculate_risk_scores(findings)
        assert result[0].risk_score < 5.0

    def test_score_bounded_at_10(self) -> None:
        findings = [self._make_finding(FindingSeverity.CRITICAL, 1.0)]
        result = _calculate_risk_scores(findings)
        assert result[0].risk_score <= 10.0

    def test_confidence_scales_score(self) -> None:
        high_conf = self._make_finding(FindingSeverity.HIGH, 1.0)
        low_conf = self._make_finding(FindingSeverity.HIGH, 0.1)
        result = _calculate_risk_scores([high_conf, low_conf])
        assert result[0].risk_score > result[1].risk_score


# ---------------------------------------------------------------------------
# Tests: LogAnalysisAgent (mit Mock-LLM)
# ---------------------------------------------------------------------------

class TestLogAnalysisAgent:
    @pytest.mark.asyncio
    async def test_run_returns_workflow_result(
        self, mock_llm: MagicMock, agent_config: AgentRunConfig
    ) -> None:
        agent = LogAnalysisAgent(mock_llm, agent_config)
        result = await agent.run(
            context={
                "raw_logs": [
                    {
                        "source": "ha_core",
                        "text": (
                            "2024-11-15 10:00:00.000 ERROR (MainThread) "
                            "[homeassistant.components.mqtt] Cannot connect to MQTT broker\n"
                        ),
                    }
                ]
            }
        )
        assert result.success is True
        assert result.agent_type == AgentType.LOG_ANALYSIS

    @pytest.mark.asyncio
    async def test_run_with_empty_logs(
        self, mock_llm: MagicMock, agent_config: AgentRunConfig
    ) -> None:
        agent = LogAnalysisAgent(mock_llm, agent_config)
        result = await agent.run(context={"raw_logs": []})
        assert result.success is True

    @pytest.mark.asyncio
    async def test_llm_called_for_error_logs(
        self, mock_llm: MagicMock, agent_config: AgentRunConfig
    ) -> None:
        agent = LogAnalysisAgent(mock_llm, agent_config)
        await agent.run(
            context={
                "raw_logs": [
                    {
                        "source": "ha_core",
                        "text": (
                            "2024-11-15 10:00:00.000 ERROR (MainThread) "
                            "[homeassistant.components.mqtt] Critical failure\n"
                        ),
                    }
                ]
            }
        )
        mock_llm.complete.assert_called()
