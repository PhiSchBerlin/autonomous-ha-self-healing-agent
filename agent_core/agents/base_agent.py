"""Abstrakte Basisklasse für alle spezialisierten Agenten."""

from abc import ABC, abstractmethod
from typing import Any
from uuid import uuid4

from langgraph.graph import StateGraph
from langgraph.graph.state import CompiledStateGraph

from llm_backends.base import BaseLLMBackend, LLMMessage
from models.agent_models import AgentRunConfig, AgentState, WorkflowResult
from models.enums import AgentStatus, AgentType


class BaseAgent(ABC):
    """
    Abstrakte Basisklasse für alle Agenten im System.

    Jeder Agent definiert seinen eigenen LangGraph-Workflow und implementiert
    die jeweiligen Analyse- und Reparaturlogiken. Der State wird über den
    gesamten Workflow-Lauf persistent gehalten.
    """

    agent_type: AgentType

    def __init__(
        self,
        llm: BaseLLMBackend,
        config: AgentRunConfig,
    ) -> None:
        self.llm = llm
        self.config = config
        self._graph: CompiledStateGraph | None = None

    @abstractmethod
    def _build_graph(self) -> CompiledStateGraph:
        """Baut und kompiliert den LangGraph-Workflow für diesen Agenten."""

    def get_graph(self) -> CompiledStateGraph:
        if self._graph is None:
            self._graph = self._build_graph()
        return self._graph

    def _initial_state(self) -> AgentState:
        return AgentState(
            run_id=uuid4(),
            agent_type=self.agent_type,
            status=AgentStatus.RUNNING,
            mode=self.config.mode,
        )

    async def run(self, context: dict[str, Any] | None = None) -> WorkflowResult:
        """
        Führt den vollständigen Agenten-Workflow aus und gibt das Ergebnis zurück.

        Der graph.ainvoke()-Aufruf durchläuft alle Knoten des StateGraph sequenziell
        oder bedingt, je nach definierten Kanten.
        """
        import time

        state = self._initial_state()
        if context:
            state.context.update(context)

        start = time.monotonic()
        graph = self.get_graph()

        thread_config = {"configurable": {"thread_id": str(state.run_id)}}
        raw_result = await graph.ainvoke(state, thread_config)
        duration = time.monotonic() - start

        # LangGraph gibt bei Pydantic-States entweder das Objekt oder ein Dict zurück
        if isinstance(raw_result, AgentState):
            final_state = raw_result
        else:
            final_state = AgentState(**raw_result)

        return WorkflowResult(
            run_id=state.run_id,
            agent_type=self.agent_type,
            success=final_state.status != AgentStatus.ERROR,
            findings_count=len(final_state.findings),
            security_issues_count=len(final_state.security_issues),
            repairs_applied_count=sum(
                1 for r in final_state.repair_actions if r.status == "applied"
            ),
            repairs_proposed_count=len(final_state.repair_actions),
            duration_seconds=duration,
            llm_calls=len(final_state.messages),
            tool_calls=len(final_state.tool_calls),
            errors=final_state.errors,
            audit_trail=[r.id for r in final_state.audit_records],
        )

    async def _call_llm(
        self,
        state: AgentState,
        user_message: str,
        system_prompt: str | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> str:
        """Hilfsmethod: LLM aufrufen und Antwort zurückgeben."""
        import logging as _logging

        messages = [LLMMessage(role="user", content=user_message)]
        try:
            response = await self.llm.complete(messages, system_prompt=system_prompt, tools=tools)
        except Exception as exc:
            _logging.getLogger(__name__).warning(
                "LLM-Aufruf fehlgeschlagen (%s) — überspringe LLM-Schritt", exc
            )
            state.errors.append(str(exc))
            return ""
        state.messages.append({
            "role": "assistant",
            "content": response.content,
            "tokens": response.total_tokens,
        })
        return response.content
