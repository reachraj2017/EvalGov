"""
Canonical span context managers.
Use these to wrap agent lifecycle boundaries.

Usage:
    with agent_task(agent_id="agent-1", role="researcher", task_input=query) as task:
        result = do_work()
        task.set_output(result)
        task.set_status("success")
"""

import json
from contextlib import contextmanager
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from .telemetry import get_tracer, get_run_id


# ---------------------------------------------------------------------------
# Helper span context objects
# ---------------------------------------------------------------------------


class TaskSpanContext:
    """Carrier object yielded by agent_task(). Allows lazy output/status updates."""

    def __init__(self, span: trace.Span):
        self._span = span
        self._output_set = False
        self._status_set = False

    def set_output(self, output: str) -> None:
        """Record the task's final output."""
        self._span.set_attribute("task.output", str(output))
        self._output_set = True

    def set_status(self, status: str) -> None:
        """
        Set task completion status.
        Valid values: "success", "failure", "partial"
        """
        self._span.set_attribute("task.status", status)
        self._status_set = True
        if status == "failure":
            self._span.set_status(Status(StatusCode.ERROR, status))
        else:
            self._span.set_status(Status(StatusCode.OK))

    def set_attribute(self, key: str, value: str) -> None:
        """Set an arbitrary span attribute."""
        self._span.set_attribute(key, value)


class ToolSpanContext:
    """Carrier object yielded by agent_tool_call(). Allows lazy output/success updates."""

    def __init__(self, span: trace.Span):
        self._span = span
        self._output_set = False
        self._success_set = False

    def set_output(self, result: dict) -> None:
        """Record the tool's output as a JSON string."""
        self._span.set_attribute("tool.output", json.dumps(result))
        self._output_set = True

    def set_success(self, success: bool) -> None:
        """Explicitly mark whether the tool call succeeded."""
        self._span.set_attribute("tool.success", str(success).lower())
        self._success_set = True
        if success:
            self._span.set_status(Status(StatusCode.OK))
        else:
            self._span.set_status(Status(StatusCode.ERROR))


# ---------------------------------------------------------------------------
# Context managers
# ---------------------------------------------------------------------------


@contextmanager
def agent_task(
    agent_id: str,
    role: str,
    task_input: str,
    task_id: str = None,
):
    """
    Context manager for an agent task span.

    Yields a TaskSpanContext with helper methods:
        set_output(output: str)
        set_status(status: str)   # "success" | "failure" | "partial"
        set_attribute(key, value)

    On clean exit defaults to status="success".
    On exception sets status="failure" and records the exception.
    """
    tracer = get_tracer()
    resolved_task_id = task_id or str(uuid4())

    with tracer.start_as_current_span("agent.task") as span:
        span.set_attribute("agent.id", agent_id)
        span.set_attribute("agent.role", role)
        span.set_attribute("task.id", resolved_task_id)
        span.set_attribute("task.input", str(task_input))
        span.set_attribute("run.id", get_run_id())

        ctx = TaskSpanContext(span)
        try:
            yield ctx
            # Apply defaults after the block finishes cleanly
            if not ctx._status_set:
                ctx.set_status("success")
        except Exception as exc:
            span.record_exception(exc)
            if not ctx._status_set:
                ctx.set_status("failure")
            raise


@contextmanager
def agent_tool_call(
    agent_id: str,
    tool_name: str,
    tool_input: dict,
):
    """
    Context manager for a single tool call span.

    Yields a ToolSpanContext with helper methods:
        set_output(result: dict)
        set_success(success: bool)

    On clean exit defaults to tool.success="true".
    On exception sets tool.success="false".
    """
    tracer = get_tracer()

    with tracer.start_as_current_span("agent.tool_call") as span:
        span.set_attribute("agent.id", agent_id)
        span.set_attribute("tool.name", tool_name)
        span.set_attribute("tool.input", json.dumps(tool_input))

        ctx = ToolSpanContext(span)
        try:
            yield ctx
            if not ctx._success_set:
                ctx.set_success(True)
        except Exception as exc:
            span.record_exception(exc)
            if not ctx._success_set:
                ctx.set_success(False)
            raise


@contextmanager
def agent_handoff(
    from_agent_id: str,
    to_agent_id: str,
    reason: str,
    context_payload: dict = None,
):
    """
    Context manager for an agent handoff span.

    Yields the raw OTel span so callers can add custom attributes if needed.
    """
    tracer = get_tracer()

    with tracer.start_as_current_span("agent.handoff") as span:
        span.set_attribute("from.agent_id", from_agent_id)
        span.set_attribute("to.agent_id", to_agent_id)
        span.set_attribute("handoff.reason", reason)
        span.set_attribute(
            "handoff.context",
            json.dumps(context_payload) if context_payload is not None else "{}",
        )
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise


@contextmanager
def agent_memory(
    agent_id: str,
    op: str,
    key: str,
    scope: str = "local",
):
    """
    Context manager for a memory read/write span.

    Args:
        agent_id: ID of the agent performing the memory operation.
        op: "read" or "write".
        key: Memory key/identifier being accessed.
        scope: "local", "shared", or "long_term".

    Yields the raw OTel span.
    """
    tracer = get_tracer()

    with tracer.start_as_current_span("agent.memory") as span:
        span.set_attribute("agent.id", agent_id)
        span.set_attribute("memory.op", op)
        span.set_attribute("memory.key", key)
        span.set_attribute("memory.scope", scope)
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise


@contextmanager
def agent_decision(
    agent_id: str,
    decision_input: str,
    options: list,
    chosen: str,
    reason: str,
):
    """
    Context manager for a decision span.

    Args:
        agent_id: ID of the agent making the decision.
        decision_input: The input/prompt that led to this decision.
        options: List of options that were considered.
        chosen: The option that was selected.
        reason: Explanation for why this option was chosen.

    Yields the raw OTel span.
    """
    tracer = get_tracer()

    with tracer.start_as_current_span("agent.decision") as span:
        span.set_attribute("agent.id", agent_id)
        span.set_attribute("decision.input", str(decision_input))
        span.set_attribute("decision.options", json.dumps(options))
        span.set_attribute("decision.chosen", str(chosen))
        span.set_attribute("decision.reason", str(reason))
        try:
            yield span
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(Status(StatusCode.ERROR))
            raise
