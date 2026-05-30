"""
Claude Agent SDK / Anthropic SDK evaluation adapter.
Wraps agent execution with canonical spans.

Usage:
    from instrumentation.adapters import ClaudeEvalAdapter

    adapter = ClaudeEvalAdapter(agent_id="claude-1", role="assistant")

    with adapter.task("Summarize this document", run_id="run-001") as task:
        result = client.messages.create(...)  # OpenLLMetry auto-instruments this
        task.set_output(result.content[0].text)
"""

from contextlib import contextmanager
from typing import Dict, Optional

from ..spans import agent_task, agent_tool_call, agent_handoff
from ..telemetry import get_run_id


class ClaudeEvalAdapter:
    """
    Thin adapter for the Anthropic / Claude SDK.

    Provides convenience context managers that delegate to the canonical
    span helpers in ``instrumentation.spans``.  LLM calls made through
    ``anthropic.Anthropic().messages.create()`` are automatically instrumented
    by OpenLLMetry (set up in ``init_telemetry``), so you only need to wrap
    the agent-level boundaries.
    """

    def __init__(self, agent_id: str, role: str = "assistant"):
        """
        Args:
            agent_id: Stable identifier for this Claude agent instance.
            role: Semantic role label (e.g. "researcher", "writer", "planner").
        """
        self.agent_id = agent_id
        self.role = role

    # -----------------------------------------------------------------------
    # Context managers
    # -----------------------------------------------------------------------

    @contextmanager
    def task(self, task_input: str, task_id: Optional[str] = None):
        """
        Context manager that opens an ``agent.task`` span around a unit of
        Claude agent work.

        Yields a ``TaskSpanContext`` with:
            set_output(output: str)
            set_status(status: str)   # "success" | "failure" | "partial"
            set_attribute(key, value)

        Example::

            with adapter.task("Write a summary of X") as task:
                result = client.messages.create(...)
                task.set_output(result.content[0].text)

        Args:
            task_input: The prompt or goal given to this agent.
            task_id: Optional explicit task identifier; auto-generated if omitted.
        """
        with agent_task(
            agent_id=self.agent_id,
            role=self.role,
            task_input=task_input,
            task_id=task_id,
        ) as ctx:
            yield ctx

    @contextmanager
    def tool_call(self, tool_name: str, tool_input: Dict):
        """
        Context manager that opens an ``agent.tool_call`` span around a tool
        invocation by this Claude agent.

        Yields a ``ToolSpanContext`` with:
            set_output(result: dict)
            set_success(success: bool)

        Example::

            with adapter.tool_call("web_search", {"query": "AI news"}) as tool:
                result = run_web_search("AI news")
                tool.set_output({"results": result})

        Args:
            tool_name: Name of the tool being called.
            tool_input: Input arguments for the tool (must be JSON-serialisable).
        """
        with agent_tool_call(
            agent_id=self.agent_id,
            tool_name=tool_name,
            tool_input=tool_input,
        ) as ctx:
            yield ctx

    @contextmanager
    def handoff(
        self,
        to_agent_id: str,
        reason: str,
        context_payload: Optional[Dict] = None,
    ):
        """
        Context manager that opens an ``agent.handoff`` span, representing this
        agent passing control (and optionally, context) to another agent.

        Yields the raw OTel span so callers can add custom attributes.

        Example::

            with adapter.handoff(
                to_agent_id="writer-1",
                reason="research_complete",
                context_payload={"summary": research_result},
            ):
                writer_agent.run(research_result)

        Args:
            to_agent_id: The agent receiving the handoff.
            reason: Short label describing why the handoff is happening.
            context_payload: Optional dict of data to pass along; serialised to JSON.
        """
        with agent_handoff(
            from_agent_id=self.agent_id,
            to_agent_id=to_agent_id,
            reason=reason,
            context_payload=context_payload,
        ) as span:
            yield span
