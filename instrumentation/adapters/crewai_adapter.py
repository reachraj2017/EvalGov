"""
CrewAI evaluation adapter.
Uses CrewAI task callbacks to emit canonical spans.

Usage:
    from instrumentation.adapters import CrewAIEvalAdapter

    adapter = CrewAIEvalAdapter()
    task = Task(
        description="...",
        callback=adapter.task_callback,
        agent=my_agent
    )
"""

import json
from typing import Any, Callable, Optional
from uuid import uuid4

from opentelemetry.trace import Status, StatusCode

from ..telemetry import get_tracer, get_run_id

try:
    import crewai  # noqa: F401
    _CREWAI_AVAILABLE = True
except ImportError:
    _CREWAI_AVAILABLE = False


class CrewAIEvalAdapter:
    """
    Adapter for CrewAI agents.

    Provides two integration points:
      1. ``task_callback`` - pass as ``callback=`` to a CrewAI ``Task``.
         Called once when the task completes; opens and immediately closes
         an ``agent.task`` span.
      2. ``get_agent_step_callback`` - returns a step callback for a specific
         agent; wraps each agent reasoning step in an ``agent.task`` span.
    """

    def __init__(self):
        self._tracer = get_tracer()

    # -----------------------------------------------------------------------
    # Task-level callback
    # -----------------------------------------------------------------------

    def task_callback(self, output: Any) -> None:
        """
        Callback invoked by CrewAI when a task finishes.

        Pass this directly as the ``callback`` kwarg of a CrewAI ``Task``.
        The span is opened and immediately closed inside this method because
        CrewAI callbacks are synchronous and fire after execution is complete.

        Args:
            output: The ``TaskOutput`` object (or plain string) from CrewAI.
        """
        # Extract fields from CrewAI TaskOutput if available
        agent_id = "crewai_agent"
        agent_role = "agent"
        task_description = ""
        task_result = ""

        if hasattr(output, "agent"):
            agent_obj = output.agent
            agent_id = getattr(agent_obj, "id", None) or getattr(
                agent_obj, "name", "crewai_agent"
            )
            agent_role = getattr(agent_obj, "role", "agent")

        if hasattr(output, "description"):
            task_description = str(output.description)
        if hasattr(output, "raw"):
            task_result = str(output.raw)
        elif hasattr(output, "result"):
            task_result = str(output.result)
        elif isinstance(output, str):
            task_result = output
        else:
            task_result = str(output)

        span = self._tracer.start_span("agent.task")
        span.set_attribute("agent.id", agent_id)
        span.set_attribute("agent.role", agent_role)
        span.set_attribute("task.id", str(uuid4()))
        span.set_attribute("task.input", task_description)
        span.set_attribute("task.output", task_result)
        span.set_attribute("task.status", "success")
        span.set_attribute("run.id", get_run_id())
        span.set_status(Status(StatusCode.OK))
        span.end()

    # -----------------------------------------------------------------------
    # Step-level callback factory
    # -----------------------------------------------------------------------

    def get_agent_step_callback(
        self, agent_id: str, role: str = "agent"
    ) -> Callable[[Any], None]:
        """
        Return a step callback function for a specific agent.

        Assign the returned callable to the ``step_callback`` attribute of a
        CrewAI ``Agent``.  It will be invoked after every agent reasoning step
        with a ``AgentAction`` / ``AgentFinish`` object.

        Args:
            agent_id: Stable identifier for this agent in evaluation traces.
            role: Semantic role label for this agent.

        Returns:
            A callable suitable for ``crewai.Agent(step_callback=...)``.
        """
        tracer = self._tracer
        run_id_fn = get_run_id

        def step_callback(step_output: Any) -> None:
            """Emit an agent.task span for a single agent step."""
            # Determine input and output from the step object
            step_input = ""
            step_result = ""

            # AgentAction has .tool, .tool_input, .log
            if hasattr(step_output, "tool"):
                step_input = (
                    f"tool={step_output.tool} "
                    f"input={getattr(step_output, 'tool_input', '')}"
                )
                step_result = getattr(step_output, "log", "")
            # AgentFinish has .return_values, .log
            elif hasattr(step_output, "return_values"):
                rv = step_output.return_values
                step_input = getattr(step_output, "log", "")
                step_result = (
                    rv.get("output", json.dumps(rv))
                    if isinstance(rv, dict)
                    else str(rv)
                )
            else:
                step_result = str(step_output)

            span = tracer.start_span("agent.task")
            span.set_attribute("agent.id", agent_id)
            span.set_attribute("agent.role", role)
            span.set_attribute("task.id", str(uuid4()))
            span.set_attribute("task.input", str(step_input))
            span.set_attribute("task.output", str(step_result))
            span.set_attribute("task.status", "success")
            span.set_attribute("run.id", run_id_fn())
            span.set_status(Status(StatusCode.OK))
            span.end()

        return step_callback
