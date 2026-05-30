"""
OpenAI Agents SDK evaluation adapter.
Implements the AgentHooks interface to emit canonical spans for OpenAI Agents SDK agents.

Usage (single agent):
    from instrumentation.adapters import OpenAIAgentsEvalAdapter
    from agents import Agent, Runner

    adapter = OpenAIAgentsEvalAdapter(agent_id="oai-1", agent_role="assistant")
    agent = Agent(name="assistant", instructions="...", hooks=adapter.hooks())
    result = Runner.run_sync(agent, "What is the capital of France?")

Usage (multi-agent handoff):
    orchestrator_adapter = OpenAIAgentsEvalAdapter(agent_id="oai-orchestrator", agent_role="orchestrator")
    searcher_adapter     = OpenAIAgentsEvalAdapter(agent_id="oai-searcher",     agent_role="searcher")

    orchestrator = Agent(name="orchestrator", ..., hooks=orchestrator_adapter.hooks())
    searcher     = Agent(name="searcher",     ..., hooks=searcher_adapter.hooks())

Usage (trace-level wrapper — wraps a full Runner.run call):
    async with adapter.trace_run("user question"):
        result = await Runner.run(agent, "user question")

Emitted span names:
    agent.task       — one per agent invocation
    agent.tool_call  — one per tool call
    agent.handoff    — one per agent-to-agent transfer
"""

import json
import logging
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from opentelemetry.trace import Status, StatusCode

from ..telemetry import get_tracer, get_run_id

log = logging.getLogger(__name__)

try:
    from agents import AgentHooks, RunContextWrapper, Tool, Agent
    _OPENAI_AGENTS_AVAILABLE = True
except ImportError:
    _OPENAI_AGENTS_AVAILABLE = False
    AgentHooks = object


class _OAIHooks(AgentHooks if _OPENAI_AGENTS_AVAILABLE else object):
    """Internal hooks implementation — do not use directly. Get via adapter.hooks()."""

    def __init__(self, adapter: "OpenAIAgentsEvalAdapter") -> None:
        self._adapter = adapter
        self._task_span_stack: list = []

    # ── Agent lifecycle ────────────────────────────────────────────────────────

    async def on_start(self, context: "RunContextWrapper", agent: "Agent") -> None:
        tracer = get_tracer()
        span = tracer.start_span("agent.task")
        span.set_attribute("agent.id",    self._adapter.agent_id)
        span.set_attribute("agent.role",  self._adapter.agent_role)
        span.set_attribute("task.id",     str(uuid4()))
        span.set_attribute("run.id",      get_run_id())
        span.set_attribute("agent.name",  getattr(agent, "name", ""))
        input_text = str(getattr(context, "input", "") or "")
        span.set_attribute("task.input",  input_text[:4096])
        self._task_span_stack.append(span)

    async def on_end(
        self,
        context: "RunContextWrapper",
        agent: "Agent",
        output: Any,
    ) -> None:
        if not self._task_span_stack:
            return
        span = self._task_span_stack.pop()
        try:
            out_str = str(output) if output is not None else ""
            span.set_attribute("task.output", out_str[:4096])
            span.set_attribute("task.status", "success")
            span.set_status(Status(StatusCode.OK))
        finally:
            span.end()

    async def on_error(
        self,
        context: "RunContextWrapper",
        agent: "Agent",
        error: Exception,
    ) -> None:
        if not self._task_span_stack:
            return
        span = self._task_span_stack[-1]
        span.record_exception(error)
        span.set_attribute("task.status", "failure")
        span.set_status(Status(StatusCode.ERROR, str(error)))

    # ── Tool lifecycle ─────────────────────────────────────────────────────────

    async def on_tool_start(
        self,
        context: "RunContextWrapper",
        agent: "Agent",
        tool: "Tool",
    ) -> None:
        tracer = get_tracer()
        tool_name = getattr(tool, "name", str(tool))
        span = tracer.start_span("agent.tool_call")
        span.set_attribute("agent.id",   self._adapter.agent_id)
        span.set_attribute("tool.name",  tool_name)
        try:
            input_dict = dict(getattr(context, "tool_input", {}) or {})
            span.set_attribute("tool.input", json.dumps(input_dict)[:2048])
        except Exception:
            pass
        # Stash on context so on_tool_end can retrieve it
        if not hasattr(context, "_oai_tool_spans"):
            context._oai_tool_spans = []
        context._oai_tool_spans.append(span)

    async def on_tool_end(
        self,
        context: "RunContextWrapper",
        agent: "Agent",
        tool: "Tool",
        result: str,
    ) -> None:
        tool_spans = getattr(context, "_oai_tool_spans", [])
        if not tool_spans:
            return
        span = tool_spans.pop()
        try:
            span.set_attribute("tool.output",  str(result)[:2048])
            span.set_attribute("tool.success", "true")
            span.set_status(Status(StatusCode.OK))
        finally:
            span.end()

    # ── Handoff ────────────────────────────────────────────────────────────────

    async def on_handoff(
        self,
        context: "RunContextWrapper",
        from_agent: "Agent",
        to_agent: "Agent",
    ) -> None:
        tracer = get_tracer()
        span = tracer.start_span("agent.handoff")
        try:
            span.set_attribute("from.agent_id", self._adapter.agent_id)
            span.set_attribute("from.agent_name", getattr(from_agent, "name", ""))
            span.set_attribute("to.agent_id",   getattr(to_agent,   "name", ""))
            span.set_attribute("to.agent_name", getattr(to_agent,   "name", ""))
            span.set_attribute("handoff.reason",  "agent_transfer")
            span.set_attribute("handoff.context", "{}")
            span.set_status(Status(StatusCode.OK))
        finally:
            span.end()


class OpenAIAgentsEvalAdapter:
    """
    Evaluation adapter for the OpenAI Agents SDK.

    Attaches to agent lifecycle hooks to emit canonical OTel spans.
    Compatible with single-agent and multi-agent (handoff) patterns.

    Args:
        agent_id:    Unique identifier for this agent instance.
        agent_role:  Semantic role (e.g. "orchestrator", "searcher", "summarizer").
    """

    def __init__(self, agent_id: str, agent_role: str) -> None:
        if not _OPENAI_AGENTS_AVAILABLE:
            log.warning(
                "openai-agents package not installed — OpenAIAgentsEvalAdapter is a no-op. "
                "Install with: pip install 'opt-aieval[openai-agents]'"
            )
        self.agent_id   = agent_id
        self.agent_role = agent_role
        self._hooks     = _OAIHooks(self)

    def hooks(self) -> "_OAIHooks":
        """Return the hooks object to pass as Agent(hooks=...) argument."""
        return self._hooks

    @asynccontextmanager
    async def trace_run(self, task_input: str, task_id: str = None):
        """
        Async context manager that wraps a full Runner.run() call in an agent.task span.

        Use when you want a single top-level span covering the entire multi-agent run
        rather than per-agent spans via hooks.

        Example:
            async with adapter.trace_run("search for AI news") as task:
                result = await Runner.run(agent, "search for AI news")
                task.set_output(str(result.final_output))
        """
        from ..spans import agent_task
        async with _async_agent_task(
            agent_id=self.agent_id,
            role=self.agent_role,
            task_input=task_input,
            task_id=task_id or str(uuid4()),
        ) as ctx:
            yield ctx


@asynccontextmanager
async def _async_agent_task(agent_id, role, task_input, task_id):
    """Async variant of the agent_task span context manager."""
    from ..spans import TaskSpanContext
    tracer = get_tracer()
    with tracer.start_as_current_span("agent.task") as span:
        span.set_attribute("agent.id",   agent_id)
        span.set_attribute("agent.role", role)
        span.set_attribute("task.id",    task_id)
        span.set_attribute("task.input", str(task_input))
        span.set_attribute("run.id",     get_run_id())
        ctx = TaskSpanContext(span)
        try:
            yield ctx
            if not ctx._status_set:
                ctx.set_status("success")
        except Exception as exc:
            span.record_exception(exc)
            if not ctx._status_set:
                ctx.set_status("failure")
            raise
