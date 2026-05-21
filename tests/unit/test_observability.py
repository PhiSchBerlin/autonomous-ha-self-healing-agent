"""Unit-Tests für ObservabilityAgent und Prometheus-Metriken."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from agent_core.agents.observability_agent import ObservabilityAgent
from models.agent_models import AgentRunConfig, AgentState
from models.enums import AgentMode, AgentStatus, AgentType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def make_config(mode: AgentMode = AgentMode.ADVISORY) -> AgentRunConfig:
    return AgentRunConfig(agent_type=AgentType.OBSERVABILITY, mode=mode)


def make_agent(ha_client: Any = None) -> ObservabilityAgent:
    llm = MagicMock()
    config = make_config()
    return ObservabilityAgent(llm, config, ha_client=ha_client)


def make_state(**kwargs: Any) -> AgentState:
    return AgentState(
        run_id=uuid4(),
        agent_type=AgentType.OBSERVABILITY,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# ObservabilityAgent — Initialisierung
# ---------------------------------------------------------------------------


class TestObservabilityAgentInit:
    def test_agent_type(self):
        agent = make_agent()
        assert agent.agent_type == AgentType.OBSERVABILITY

    def test_ha_client_none_by_default(self):
        agent = make_agent()
        assert agent._ha_client is None

    def test_ha_client_stored(self):
        mock_client = MagicMock()
        agent = make_agent(ha_client=mock_client)
        assert agent._ha_client is mock_client

    def test_baseline_empty_on_init(self):
        agent = make_agent()
        assert agent._baseline == {}

    def test_set_baseline(self):
        agent = make_agent()
        agent.set_baseline({"sensor.temp": "21.5", "switch.light": "on"})
        assert len(agent._baseline) == 2
        assert agent._baseline["sensor.temp"] == "21.5"

    def test_set_baseline_copies_dict(self):
        agent = make_agent()
        original = {"sensor.temp": "21.5"}
        agent.set_baseline(original)
        original["sensor.new"] = "added"
        assert "sensor.new" not in agent._baseline

    def test_graph_builds(self):
        agent = make_agent()
        graph = agent.get_graph()
        assert graph is not None

    def test_get_health_summary(self):
        agent = make_agent()
        agent.set_baseline({"a": "1", "b": "2"})
        summary = agent.get_health_summary()
        assert summary["baseline_entities"] == 2
        assert summary["agent_type"] == AgentType.OBSERVABILITY


# ---------------------------------------------------------------------------
# _node_collect_ha_state
# ---------------------------------------------------------------------------


class TestCollectHaState:
    @pytest.mark.asyncio
    async def test_with_ha_client(self):
        mock_client = AsyncMock()
        mock_client.get_all_states.return_value = [
            {"entity_id": "sensor.temp", "state": "21.5"},
            {"entity_id": "switch.light", "state": "on"},
        ]
        agent = make_agent(ha_client=mock_client)
        state = make_state()

        result = await agent._node_collect_ha_state(state)

        assert result.context["ha_reachable"] is True
        assert "sensor.temp" in result.context["current_ha_state"]
        assert result.context["current_ha_state"]["sensor.temp"] == "21.5"

    @pytest.mark.asyncio
    async def test_without_ha_client_uses_context(self):
        agent = make_agent()
        state = make_state()
        state.context["ha_reachable"] = True
        state.context["current_ha_state"] = {"sensor.x": "ok"}

        result = await agent._node_collect_ha_state(state)

        assert result.context["ha_reachable"] is True
        assert "sensor.x" in result.context["current_ha_state"]

    @pytest.mark.asyncio
    async def test_ha_client_unreachable(self):
        mock_client = AsyncMock()
        mock_client.get_all_states.side_effect = ConnectionError("refused")
        agent = make_agent(ha_client=mock_client)
        state = make_state()

        result = await agent._node_collect_ha_state(state)

        assert result.context["ha_reachable"] is False
        assert result.context["current_ha_state"] == {}

    @pytest.mark.asyncio
    async def test_collected_at_set(self):
        agent = make_agent()
        state = make_state()

        result = await agent._node_collect_ha_state(state)

        assert "collected_at" in result.context

    @pytest.mark.asyncio
    async def test_entity_without_entity_id_skipped(self):
        mock_client = AsyncMock()
        mock_client.get_all_states.return_value = [
            {"state": "ok"},  # kein entity_id
            {"entity_id": "sensor.valid", "state": "21"},
        ]
        agent = make_agent(ha_client=mock_client)
        state = make_state()

        result = await agent._node_collect_ha_state(state)

        assert len(result.context["current_ha_state"]) == 1
        assert "sensor.valid" in result.context["current_ha_state"]


# ---------------------------------------------------------------------------
# _node_evaluate_health
# ---------------------------------------------------------------------------


class TestEvaluateHealth:
    @pytest.mark.asyncio
    async def test_perfect_health_when_all_ok(self):
        agent = make_agent()
        state = make_state()
        state.context = {
            "ha_reachable": True,
            "current_ha_state": {"sensor.a": "21", "sensor.b": "on"},
        }

        result = await agent._node_evaluate_health(state)

        assert result.context["health_score"] == 1.0
        assert result.context["unavailable_count"] == 0

    @pytest.mark.asyncio
    async def test_zero_health_when_unreachable(self):
        agent = make_agent()
        state = make_state()
        state.context = {
            "ha_reachable": False,
            "current_ha_state": {},
        }

        result = await agent._node_evaluate_health(state)

        assert result.context["health_score"] == 0.0

    @pytest.mark.asyncio
    async def test_partial_health_with_unavailable(self):
        agent = make_agent()
        state = make_state()
        state.context = {
            "ha_reachable": True,
            "current_ha_state": {
                "sensor.ok": "21",
                "sensor.bad": "unavailable",
            },
        }

        result = await agent._node_evaluate_health(state)

        # 50% unavailable → health_score = 1.0 - 0.5 * 0.5 = 0.75
        assert result.context["health_score"] == 0.75
        assert "sensor.bad" in result.context["unavailable_entities"]

    @pytest.mark.asyncio
    async def test_unknown_state_counts_as_unavailable(self):
        agent = make_agent()
        state = make_state()
        state.context = {
            "ha_reachable": True,
            "current_ha_state": {"sensor.x": "unknown"},
        }

        result = await agent._node_evaluate_health(state)

        assert result.context["unavailable_count"] == 1

    @pytest.mark.asyncio
    async def test_empty_state_gives_full_health(self):
        agent = make_agent()
        state = make_state()
        state.context = {
            "ha_reachable": True,
            "current_ha_state": {},
        }

        result = await agent._node_evaluate_health(state)

        assert result.context["health_score"] == 1.0


# ---------------------------------------------------------------------------
# _node_check_regressions
# ---------------------------------------------------------------------------


class TestCheckRegressions:
    @pytest.mark.asyncio
    async def test_no_baseline_no_regressions(self):
        agent = make_agent()
        state = make_state()
        state.context = {"current_ha_state": {"sensor.x": "ok"}}

        result = await agent._node_check_regressions(state)

        assert result.context["regression_count"] == 0
        assert result.context["has_regressions"] is False

    @pytest.mark.asyncio
    async def test_detects_disappeared_entity(self):
        agent = make_agent()
        agent.set_baseline({"sensor.gone": "21", "sensor.ok": "on"})
        state = make_state()
        state.context = {"current_ha_state": {"sensor.ok": "on"}}

        result = await agent._node_check_regressions(state)

        assert result.context["regression_count"] == 1
        assert result.context["regressions"][0]["type"] == "disappeared"
        assert result.context["regressions"][0]["entity_id"] == "sensor.gone"

    @pytest.mark.asyncio
    async def test_detects_degraded_entity(self):
        agent = make_agent()
        agent.set_baseline({"sensor.temp": "21"})
        state = make_state()
        state.context = {"current_ha_state": {"sensor.temp": "unavailable"}}

        result = await agent._node_check_regressions(state)

        assert result.context["regression_count"] == 1
        assert result.context["regressions"][0]["type"] == "degraded"

    @pytest.mark.asyncio
    async def test_no_regression_when_already_unavailable_in_baseline(self):
        agent = make_agent()
        # Entity war schon in Baseline unavailable — keine Regression
        agent.set_baseline({"sensor.bad": "unavailable"})
        state = make_state()
        state.context = {"current_ha_state": {"sensor.bad": "unavailable"}}

        result = await agent._node_check_regressions(state)

        assert result.context["regression_count"] == 0

    @pytest.mark.asyncio
    async def test_no_regression_when_all_stable(self):
        agent = make_agent()
        agent.set_baseline({"sensor.a": "21", "sensor.b": "on"})
        state = make_state()
        state.context = {"current_ha_state": {"sensor.a": "22", "sensor.b": "off"}}

        result = await agent._node_check_regressions(state)

        # Zustandsänderung ist keine Regression — nur unavailable/disappear ist Regression
        assert result.context["regression_count"] == 0


# ---------------------------------------------------------------------------
# _node_done
# ---------------------------------------------------------------------------


class TestNodeDone:
    @pytest.mark.asyncio
    async def test_idle_when_healthy(self):
        agent = make_agent()
        state = make_state()
        state.context = {"regression_count": 0, "health_score": 1.0}

        result = await agent._node_done(state)

        assert result.status == AgentStatus.IDLE

    @pytest.mark.asyncio
    async def test_error_when_regressions(self):
        agent = make_agent()
        state = make_state()
        state.context = {"regression_count": 2, "health_score": 0.9}

        result = await agent._node_done(state)

        assert result.status == AgentStatus.ERROR
        assert result.errors

    @pytest.mark.asyncio
    async def test_error_when_low_health_score(self):
        agent = make_agent()
        state = make_state()
        state.context = {"regression_count": 0, "health_score": 0.3}

        result = await agent._node_done(state)

        assert result.status == AgentStatus.ERROR


# ---------------------------------------------------------------------------
# Prometheus-Metriken
# ---------------------------------------------------------------------------


class TestPrometheusMetrics:
    def test_import_metrics_module(self):
        from observability import metrics  # noqa: F401
        assert True

    def test_record_finding(self):
        from observability.metrics import record_finding
        record_finding("high", "security")  # darf nicht werfen

    def test_resolve_finding(self):
        from observability.metrics import resolve_finding
        resolve_finding("high")  # darf nicht werfen

    def test_record_repair_proposed(self):
        from observability.metrics import record_repair_proposed
        record_repair_proposed("medium")

    def test_record_repair_applied(self):
        from observability.metrics import record_repair_applied
        record_repair_applied("applied", "low")

    def test_record_security_issue(self):
        from observability.metrics import record_security_issue
        record_security_issue("secrets_detection", "critical")

    def test_generate_metrics_output_returns_tuple(self):
        from observability.metrics import generate_metrics_output
        content, content_type = generate_metrics_output()
        assert isinstance(content, bytes)
        assert isinstance(content_type, str)
        assert len(content) > 0

    def test_track_agent_run_context_manager(self):
        from observability.metrics import track_agent_run
        with track_agent_run("log_analysis", "advisory"):
            pass  # kein Fehler

    def test_track_agent_run_records_error_on_exception(self):
        from observability.metrics import track_agent_run
        with pytest.raises(ValueError):
            with track_agent_run("repair", "autonomous"):
                raise ValueError("test error")

    def test_stub_metric_methods(self):
        from observability.metrics import _StubMetric
        stub = _StubMetric()
        stub.inc()
        stub.dec()
        stub.set(1.0)
        stub.observe(0.5)
        labeled = stub.labels(severity="high")
        assert labeled is stub

    def test_stub_metric_context_manager(self):
        from observability.metrics import _StubMetric
        stub = _StubMetric()
        with stub:
            pass


# ---------------------------------------------------------------------------
# ObservabilityAgent — Orchestrator-Integration
# ---------------------------------------------------------------------------


class TestOrchestratorObservability:
    def test_get_observability_agent(self):
        from agent_core.orchestrator.orchestrator import AgentOrchestrator
        from models.enums import AgentMode
        from unittest.mock import MagicMock

        llm = MagicMock()
        orch = AgentOrchestrator(llm, mode=AgentMode.ADVISORY)
        agent = orch.get_observability_agent()
        assert isinstance(agent, ObservabilityAgent)

    def test_get_observability_agent_with_client(self):
        from agent_core.orchestrator.orchestrator import AgentOrchestrator
        from models.enums import AgentMode
        from unittest.mock import MagicMock

        llm = MagicMock()
        mock_client = MagicMock()
        orch = AgentOrchestrator(llm, mode=AgentMode.ADVISORY)
        agent = orch.get_observability_agent(ha_client=mock_client)
        assert agent._ha_client is mock_client
