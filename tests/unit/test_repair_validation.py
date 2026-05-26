"""Unit-Tests für Repair-Agent und Validation-Agent."""

from __future__ import annotations

import difflib
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from agent_core.agents.repair_agent import RepairAgent, _generate_simple_diff
from agent_core.agents.validation_agent import ValidationAgent
from llm_backends.base import LLMBackendConfig, LLMResponse
from models.agent_models import AgentRunConfig, AgentState
from models.enums import (
    AgentMode,
    AgentStatus,
    AgentType,
    FindingCategory,
    FindingSeverity,
    RepairStatus,
)
from models.log_models import Finding
from models.repair_models import FileChange, RepairAction


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_llm_approved() -> MagicMock:
    """LLM das einen gültigen, approbierten Patch zurückgibt."""
    llm = MagicMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            content="""
{
  "approved": true,
  "confidence": 0.95,
  "issues": [],
  "suggestions": []
}
""",
            model="test-model",
            backend="ollama",
            total_tokens=50,
        )
    )
    llm.health_check = AsyncMock(return_value=True)
    llm.is_local = True
    llm.config = LLMBackendConfig(backend_type="ollama", model="llama3.2")
    return llm


@pytest.fixture
def mock_llm_patch() -> MagicMock:
    """LLM das einen validen Patch generiert."""
    llm = MagicMock()
    llm.complete = AsyncMock(
        return_value=LLMResponse(
            content="""
{
  "title": "Fix MQTT broker host",
  "rationale": "Hardcoded IP should use !secret",
  "changes": [
    {
      "file_path": "/config/configuration.yaml",
      "change_description": "Replace hardcoded IP with secret reference",
      "original_snippet": "host: 192.168.1.100",
      "fixed_snippet": "host: !secret mqtt_host",
      "change_type": "modify"
    }
  ],
  "risk_level": "low",
  "estimated_impact": "MQTT will use secrets file for broker address",
  "alternatives": []
}
""",
            model="test-model",
            backend="ollama",
            total_tokens=120,
        )
    )
    llm.health_check = AsyncMock(return_value=True)
    llm.is_local = True
    llm.config = LLMBackendConfig(backend_type="ollama", model="llama3.2")
    return llm


@pytest.fixture
def sample_finding() -> Finding:
    return Finding(
        title="Hardcoded MQTT Broker IP",
        description="MQTT broker IP is hardcoded in configuration.yaml",
        severity=FindingSeverity.HIGH,
        category=FindingCategory.SECURITY,
        source_agent=AgentType.SECURITY,
        confidence=0.9,
        affected_files=["/config/configuration.yaml"],
        root_cause="Direct IP instead of !secret reference",
        suggested_fix="Use !secret mqtt_host in configuration.yaml",
        risk_score=7.5,
    )


@pytest.fixture
def sample_repair_action(sample_finding: Finding) -> RepairAction:
    return RepairAction(
        finding_id=sample_finding.id,
        title="Fix hardcoded MQTT broker IP",
        description="Replace IP with !secret reference",
        rationale="Security best practice requires !secret for credentials",
        status=RepairStatus.PROPOSED,
        changes=[
            FileChange(
                file_path="/config/configuration.yaml",
                original_content="host: 192.168.1.100",
                proposed_content="host: !secret mqtt_host",
                diff="--- a/configuration.yaml\n+++ b/configuration.yaml\n-host: 192.168.1.100\n+host: !secret mqtt_host",
                change_type="modify",
            )
        ],
        confidence=0.9,
        risk_level="low",
    )


def make_agent_state(mode: AgentMode = AgentMode.ADVISORY, **kwargs) -> AgentState:
    """Erstellt einen minimalen AgentState mit Pflichtfeldern."""
    return AgentState(
        run_id=uuid4(),
        agent_type=AgentType.REPAIR,
        mode=mode,
        **kwargs,
    )


def make_repair_config(mode: AgentMode = AgentMode.ADVISORY) -> AgentRunConfig:
    return AgentRunConfig(
        agent_type=AgentType.REPAIR,
        mode=mode,
        dry_run=mode != AgentMode.AUTONOMOUS,
    )


def make_validation_config() -> AgentRunConfig:
    return AgentRunConfig(
        agent_type=AgentType.VALIDATION,
        mode=AgentMode.ADVISORY,
        dry_run=True,
    )


# ---------------------------------------------------------------------------
# _generate_simple_diff
# ---------------------------------------------------------------------------

class TestGenerateSimpleDiff:
    def test_produces_unified_diff_format(self):
        original = "host: 192.168.1.100"
        fixed = "host: !secret mqtt_host"
        diff = _generate_simple_diff(original, fixed, "configuration.yaml")
        assert "---" in diff
        assert "+++" in diff

    def test_diff_contains_removed_line(self):
        original = "old_value: test"
        fixed = "new_value: test"
        diff = _generate_simple_diff(original, fixed)
        assert "-old_value" in diff or "old_value" in diff

    def test_diff_contains_added_line(self):
        original = "old_value: test"
        fixed = "new_value: test"
        diff = _generate_simple_diff(original, fixed)
        assert "+new_value" in diff or "new_value" in diff

    def test_identical_content_produces_empty_diff(self):
        content = "key: value"
        diff = _generate_simple_diff(content, content)
        assert diff == ""

    def test_empty_strings(self):
        diff = _generate_simple_diff("", "")
        assert diff == ""

    def test_multiline_diff(self):
        original = "line1\nline2\nline3"
        fixed = "line1\nline2_modified\nline3"
        diff = _generate_simple_diff(original, fixed, "test.yaml")
        assert len(diff) > 0


# ---------------------------------------------------------------------------
# RepairAgent — Routing-Logik
# ---------------------------------------------------------------------------

class TestRepairAgentRouting:
    def test_route_after_validate_validated_status(
        self, mock_llm_patch: MagicMock, sample_repair_action: RepairAction
    ):
        config = make_repair_config(AgentMode.AUTONOMOUS)
        agent = RepairAgent(mock_llm_patch, config)

        sample_repair_action.status = RepairStatus.VALIDATED
        state = make_agent_state(AgentMode.AUTONOMOUS, repair_actions=[sample_repair_action])
        route = agent._route_after_validate(state)
        assert route == "request_approval"

    def test_route_after_validate_failed_status(
        self, mock_llm_patch: MagicMock, sample_repair_action: RepairAction
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        sample_repair_action.status = RepairStatus.FAILED
        state = make_agent_state(AgentMode.ADVISORY, repair_actions=[sample_repair_action])
        route = agent._route_after_validate(state)
        assert route == "done"

    def test_route_after_validate_empty_repairs(self, mock_llm_patch: MagicMock):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        state = make_agent_state(AgentMode.ADVISORY)
        route = agent._route_after_validate(state)
        assert route == "done"

    def test_route_after_approval_approved(
        self, mock_llm_patch: MagicMock, sample_repair_action: RepairAction
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        sample_repair_action.status = RepairStatus.APPROVED
        state = make_agent_state(AgentMode.APPROVAL, repair_actions=[sample_repair_action])
        route = agent._route_after_approval(state)
        assert route == "backup"

    def test_route_after_approval_rejected(
        self, mock_llm_patch: MagicMock, sample_repair_action: RepairAction
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        sample_repair_action.status = RepairStatus.REJECTED
        state = make_agent_state(AgentMode.APPROVAL, repair_actions=[sample_repair_action])
        route = agent._route_after_approval(state)
        assert route == "done"

    def test_route_after_apply_applied(
        self, mock_llm_patch: MagicMock, sample_repair_action: RepairAction
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        sample_repair_action.status = RepairStatus.APPLIED
        state = make_agent_state(AgentMode.AUTONOMOUS, repair_actions=[sample_repair_action])
        route = agent._route_after_apply(state)
        assert route == "monitor"

    def test_route_after_apply_failed(
        self, mock_llm_patch: MagicMock, sample_repair_action: RepairAction
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        sample_repair_action.status = RepairStatus.FAILED
        state = make_agent_state(AgentMode.AUTONOMOUS, repair_actions=[sample_repair_action])
        route = agent._route_after_apply(state)
        assert route == "rollback"


# ---------------------------------------------------------------------------
# RepairAgent — Node-Logik
# ---------------------------------------------------------------------------

class TestRepairAgentNodes:
    @pytest.mark.asyncio
    async def test_node_analyse_finding_no_finding_returns_error(
        self, mock_llm_patch: MagicMock
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        state = make_agent_state(AgentMode.ADVISORY, context={})
        result = await agent._node_analyse_finding(state)
        assert "errors" in result
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    async def test_node_analyse_finding_with_finding_dict(
        self, mock_llm_patch: MagicMock, sample_finding: Finding
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={"finding": sample_finding.model_dump(mode="json")},
        )
        result = await agent._node_analyse_finding(state)
        # Kein Fehler wenn Finding vorhanden
        assert "errors" not in result or len(result.get("errors", [])) == 0

    @pytest.mark.asyncio
    async def test_node_analyse_finding_with_finding_object(
        self, mock_llm_patch: MagicMock, sample_finding: Finding
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={"finding": sample_finding},
        )
        result = await agent._node_analyse_finding(state)
        assert "errors" not in result or len(result.get("errors", [])) == 0

    @pytest.mark.asyncio
    async def test_node_request_approval_advisory_mode(
        self, mock_llm_patch: MagicMock, sample_repair_action: RepairAction
    ):
        """Im Advisory-Modus wird kein Approval erteilt, Status bleibt VALIDATED."""
        config = make_repair_config(AgentMode.ADVISORY)
        agent = RepairAgent(mock_llm_patch, config)

        sample_repair_action.status = RepairStatus.VALIDATED
        state = make_agent_state(AgentMode.ADVISORY, repair_actions=[sample_repair_action])
        result = await agent._node_request_approval(state)
        # Advisory-Modus: leeres Dict, Status unverändert
        assert result == {}
        assert sample_repair_action.status == RepairStatus.VALIDATED

    @pytest.mark.asyncio
    async def test_node_request_approval_autonomous_mode(
        self, mock_llm_patch: MagicMock, sample_repair_action: RepairAction
    ):
        """Im Autonomous-Modus wird automatisch genehmigt."""
        config = make_repair_config(AgentMode.AUTONOMOUS)
        agent = RepairAgent(mock_llm_patch, config)

        sample_repair_action.status = RepairStatus.VALIDATED
        state = make_agent_state(AgentMode.AUTONOMOUS, repair_actions=[sample_repair_action])
        result = await agent._node_request_approval(state)
        updated_repairs = result.get("repair_actions", [])
        approved = next((r for r in updated_repairs if r.id == sample_repair_action.id), None)
        assert approved is not None
        assert approved.status == RepairStatus.APPROVED

    @pytest.mark.asyncio
    async def test_node_done_sets_idle_status(
        self, mock_llm_patch: MagicMock
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        state = make_agent_state(AgentMode.ADVISORY)
        result = await agent._node_done(state)
        assert result.get("status") == AgentStatus.IDLE

    def test_get_current_repair_returns_last(
        self, mock_llm_patch: MagicMock, sample_repair_action: RepairAction
    ):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        second_repair = RepairAction(
            finding_id=uuid4(),
            title="Second Repair",
            description="",
            rationale="",
            status=RepairStatus.PROPOSED,
            changes=[],
            confidence=0.5,
            risk_level="low",
        )
        state = make_agent_state(
            AgentMode.ADVISORY,
            repair_actions=[sample_repair_action, second_repair],
        )
        current = agent._get_current_repair(state)
        assert current is not None
        assert current.id == second_repair.id

    def test_get_current_repair_empty_returns_none(self, mock_llm_patch: MagicMock):
        config = make_repair_config()
        agent = RepairAgent(mock_llm_patch, config)

        state = make_agent_state(AgentMode.ADVISORY)
        assert agent._get_current_repair(state) is None


# ---------------------------------------------------------------------------
# ValidationAgent — Node-Logik
# ---------------------------------------------------------------------------

class TestValidationAgentNodes:
    @pytest.mark.asyncio
    async def test_node_prepare_sandbox_no_repair_action(
        self, mock_llm_approved: MagicMock
    ):
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        state = make_agent_state(AgentMode.ADVISORY, context={})
        result = await agent._node_prepare_sandbox(state)
        assert "errors" in result
        assert len(result["errors"]) > 0

    @pytest.mark.asyncio
    async def test_node_prepare_sandbox_creates_temp_files(
        self, mock_llm_approved: MagicMock, sample_repair_action: RepairAction
    ):
        import os
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={"repair_action": sample_repair_action},
        )
        result = await agent._node_prepare_sandbox(state)
        context = result.get("context", {})

        sandbox_dir = context.get("sandbox_dir", "")
        sandbox_files = context.get("sandbox_files", {})

        assert sandbox_dir != ""
        assert len(sandbox_files) > 0

        # Temp-Dateien sollten existieren
        for orig_path, sandbox_path in sandbox_files.items():
            assert os.path.exists(sandbox_path)

        # Aufräumen
        if sandbox_dir:
            import shutil
            shutil.rmtree(sandbox_dir, ignore_errors=True)

    @pytest.mark.asyncio
    async def test_node_lint_yaml_clean_yaml_no_issues(
        self, mock_llm_approved: MagicMock, tmp_path
    ):
        import os
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        # Gültige YAML-Datei in Sandbox anlegen
        sandbox_file = tmp_path / "configuration.yaml"
        sandbox_file.write_text("homeassistant:\n  name: Test\n")

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={
                "sandbox_files": {
                    "/config/configuration.yaml": str(sandbox_file)
                },
                "validation_issues": [],
            },
        )
        result = await agent._node_lint_yaml(state)
        issues = result.get("context", {}).get("validation_issues", [])
        assert len(issues) == 0

    @pytest.mark.asyncio
    async def test_node_lint_yaml_invalid_yaml_adds_issue(
        self, mock_llm_approved: MagicMock, tmp_path
    ):
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        sandbox_file = tmp_path / "broken.yaml"
        sandbox_file.write_text("key: [\nunclosed bracket")

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={
                "sandbox_files": {
                    "/config/broken.yaml": str(sandbox_file)
                },
                "validation_issues": [],
            },
        )
        result = await agent._node_lint_yaml(state)
        issues = result.get("context", {}).get("validation_issues", [])
        assert len(issues) > 0

    @pytest.mark.asyncio
    async def test_node_check_python_valid_syntax(
        self, mock_llm_approved: MagicMock, tmp_path
    ):
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        py_file = tmp_path / "sensor.py"
        py_file.write_text("def setup_platform(hass, config, add_entities, discovery_info=None):\n    pass\n")

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={
                "sandbox_files": {"/config/custom_components/test/sensor.py": str(py_file)},
                "validation_issues": [],
            },
        )
        result = await agent._node_check_python(state)
        issues = result.get("context", {}).get("validation_issues", [])
        assert len(issues) == 0

    @pytest.mark.asyncio
    async def test_node_check_python_syntax_error_adds_issue(
        self, mock_llm_approved: MagicMock, tmp_path
    ):
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        py_file = tmp_path / "broken.py"
        py_file.write_text("def broken(\n    return None\n")

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={
                "sandbox_files": {"/config/custom_components/test/broken.py": str(py_file)},
                "validation_issues": [],
            },
        )
        result = await agent._node_check_python(state)
        issues = result.get("context", {}).get("validation_issues", [])
        assert len(issues) > 0

    @pytest.mark.asyncio
    async def test_node_generate_report_passed_when_no_issues(
        self, mock_llm_approved: MagicMock
    ):
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={
                "validation_issues": [],
                "sandbox_dir": "",
                "llm_sanity": {"verdict": "Looks good", "confidence": 0.95},
            },
        )
        result = await agent._node_generate_report(state)
        report = result.get("context", {}).get("validation_report", {})
        assert report.get("passed") is True
        assert report.get("issue_count") == 0

    @pytest.mark.asyncio
    async def test_node_generate_report_failed_when_issues(
        self, mock_llm_approved: MagicMock
    ):
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={
                "validation_issues": ["YAML-Fehler in broken.yaml Zeile 3: mapping values are not allowed"],
                "sandbox_dir": "",
            },
        )
        result = await agent._node_generate_report(state)
        report = result.get("context", {}).get("validation_report", {})
        assert report.get("passed") is False
        assert report.get("issue_count") == 1

    @pytest.mark.asyncio
    async def test_node_generate_report_cleans_up_sandbox(
        self, mock_llm_approved: MagicMock, tmp_path
    ):
        import os
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        sandbox_dir = tmp_path / "ha_agent_validation_cleanup_test"
        sandbox_dir.mkdir()
        (sandbox_dir / "test.yaml").write_text("key: value\n")

        state = make_agent_state(
            AgentMode.ADVISORY,
            context={
                "validation_issues": [],
                "sandbox_dir": str(sandbox_dir),
            },
        )
        await agent._node_generate_report(state)

        # Sandbox-Verzeichnis sollte entfernt worden sein
        assert not os.path.exists(str(sandbox_dir))

    @pytest.mark.asyncio
    async def test_node_done_returns_idle(self, mock_llm_approved: MagicMock):
        config = make_validation_config()
        agent = ValidationAgent(mock_llm_approved, config)

        state = make_agent_state(AgentMode.ADVISORY)
        result = await agent._node_done(state)
        assert result.get("status") == AgentStatus.IDLE


# ---------------------------------------------------------------------------
# RepairAction — Modell-Logik
# ---------------------------------------------------------------------------

class TestRepairActionModel:
    def test_initial_status_is_proposed(self, sample_repair_action: RepairAction):
        assert sample_repair_action.status == RepairStatus.PROPOSED

    def test_touch_updates_timestamp(self, sample_repair_action: RepairAction):
        old_updated = sample_repair_action.updated_at
        sample_repair_action.touch()
        assert sample_repair_action.updated_at >= old_updated

    def test_file_change_has_diff(self, sample_repair_action: RepairAction):
        assert len(sample_repair_action.changes) > 0
        change = sample_repair_action.changes[0]
        assert change.diff != ""

    def test_repair_action_uuid_unique(self):
        r1 = RepairAction(
            finding_id=uuid4(),
            title="Repair 1",
            description="",
            rationale="",
            status=RepairStatus.PROPOSED,
            changes=[],
            confidence=0.5,
            risk_level="low",
        )
        r2 = RepairAction(
            finding_id=uuid4(),
            title="Repair 2",
            description="",
            rationale="",
            status=RepairStatus.PROPOSED,
            changes=[],
            confidence=0.5,
            risk_level="low",
        )
        assert r1.id != r2.id
