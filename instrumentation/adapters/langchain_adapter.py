"""
LangChain evaluation adapter.
Implements BaseCallbackHandler to emit canonical spans for LangChain agents.

Usage:
    from instrumentation.adapters import LangChainEvalAdapter

    adapter = LangChainEvalAdapter(agent_id="lc-agent-1", agent_role="researcher")
    agent = create_react_agent(llm, tools, prompt)
    agent.invoke(input, config={"callbacks": [adapter]})
"""

import json
from typing import Any, Dict, List, Optional, Union
from uuid import UUID

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ..telemetry import get_tracer, get_run_id

try:
    from langchain_core.callbacks import BaseCallbackHandler
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    _LANGCHAIN_AVAILABLE = False
    BaseCallbackHandler = object  # Placeholder base class so the class body is valid


class LangChainEvalAdapter(BaseCallbackHandler):
    """
    LangChain callback handler that emits canonical aieval OTel spans.

    Attach to any LangChain agent or chain via the "callbacks" config key.
    Spans are opened on chain/tool start events and closed on end/error events.
    Multiple concurrent tool spans are tracked by LangChain's run_id.
    """

    def __init__(self, agent_id: str, agent_role: str = "agent"):
        if not _LANGCHAIN_AVAILABLE:
            raise ImportError(
                "LangChain is not installed. "
                "Run: pip install langchain-core langchain langchain-community"
            )
        super().__init__()
        self.agent_id = agent_id
        self.agent_role = agent_role
        self._tracer = get_tracer()

        # Active task span (one per chain execution)
        self._task_span: Optional[trace.Span] = None
        self._task_token = None  # context token for manual context management

        # Active tool spans keyed by LangChain run_id (UUID)
        self._tool_spans: Dict[str, trace.Span] = {}
        self._tool_tokens: Dict[str, object] = {}

    # -----------------------------------------------------------------------
    # Chain events  →  agent.task spans
    # -----------------------------------------------------------------------

    def on_chain_start(
        self,
        serialized: Dict[str, Any],
        inputs: Dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Open an agent.task span when a chain begins."""
        task_input = (
            inputs.get("input")
            or inputs.get("query")
            or json.dumps(inputs)
        )

        span = self._tracer.start_span("agent.task")
        span.set_attribute("agent.id", self.agent_id)
        span.set_attribute("agent.role", self.agent_role)
        span.set_attribute("task.id", str(run_id))
        span.set_attribute("task.input", str(task_input))
        span.set_attribute("run.id", get_run_id())

        # Activate span in context so child spans nest correctly
        ctx = trace.use_span(span, end_on_exit=False)
        token = ctx.__enter__()  # type: ignore[attr-defined]

        self._task_span = span
        self._task_token = (ctx, token)

    def on_chain_end(
        self,
        outputs: Dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Close the agent.task span successfully when the chain finishes."""
        if self._task_span is None:
            return

        task_output = (
            outputs.get("output")
            or outputs.get("result")
            or json.dumps(outputs)
        )
        self._task_span.set_attribute("task.output", str(task_output))
        self._task_span.set_attribute("task.status", "success")
        self._task_span.set_status(Status(StatusCode.OK))
        self._end_task_span()

    def on_chain_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Close the agent.task span with error status."""
        if self._task_span is None:
            return

        self._task_span.record_exception(error)
        self._task_span.set_attribute("task.status", "failure")
        self._task_span.set_status(Status(StatusCode.ERROR, str(error)))
        self._end_task_span()

    def _end_task_span(self) -> None:
        """Deactivate and end the current task span."""
        if self._task_token is not None:
            ctx, _token = self._task_token
            try:
                ctx.__exit__(None, None, None)  # type: ignore[attr-defined]
            except Exception:
                pass
            self._task_token = None
        if self._task_span is not None:
            self._task_span.end()
            self._task_span = None

    # -----------------------------------------------------------------------
    # Tool events  →  agent.tool_call spans
    # -----------------------------------------------------------------------

    def on_tool_start(
        self,
        serialized: Dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Open an agent.tool_call span when a tool is invoked."""
        tool_name = serialized.get("name", "unknown_tool")

        span = self._tracer.start_span("agent.tool_call")
        span.set_attribute("agent.id", self.agent_id)
        span.set_attribute("tool.name", tool_name)
        # input_str from LangChain is already a string; wrap in JSON object for consistency
        try:
            parsed = json.loads(input_str)
            span.set_attribute("tool.input", json.dumps(parsed))
        except (json.JSONDecodeError, TypeError):
            span.set_attribute("tool.input", json.dumps({"input": input_str}))

        run_key = str(run_id)
        ctx = trace.use_span(span, end_on_exit=False)
        token = ctx.__enter__()  # type: ignore[attr-defined]

        self._tool_spans[run_key] = span
        self._tool_tokens[run_key] = (ctx, token)

    def on_tool_end(
        self,
        output: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Close the agent.tool_call span on success."""
        run_key = str(run_id)
        span = self._tool_spans.pop(run_key, None)
        ctx_token = self._tool_tokens.pop(run_key, None)

        if span is None:
            return

        span.set_attribute("tool.output", json.dumps({"output": str(output)}))
        span.set_attribute("tool.success", "true")
        span.set_status(Status(StatusCode.OK))
        self._end_tool_span(span, ctx_token)

    def on_tool_error(
        self,
        error: Union[Exception, KeyboardInterrupt],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        """Close the agent.tool_call span on error."""
        run_key = str(run_id)
        span = self._tool_spans.pop(run_key, None)
        ctx_token = self._tool_tokens.pop(run_key, None)

        if span is None:
            return

        span.record_exception(error)
        span.set_attribute("tool.success", "false")
        span.set_status(Status(StatusCode.ERROR, str(error)))
        self._end_tool_span(span, ctx_token)

    @staticmethod
    def _end_tool_span(span: trace.Span, ctx_token) -> None:
        """Deactivate context and end a tool span."""
        if ctx_token is not None:
            ctx, _token = ctx_token
            try:
                ctx.__exit__(None, None, None)  # type: ignore[attr-defined]
            except Exception:
                pass
        span.end()
