"""
AutoGen evaluation adapter.
Wraps AutoGen agent message passing to emit canonical spans.

Usage:
    from instrumentation.adapters import AutoGenEvalAdapter

    adapter = AutoGenEvalAdapter()
    # Wrap agent before registering reply functions
    adapter.wrap_agent(my_autogen_agent, agent_id="ag-1", role="coder")
"""

import json
from typing import Any, Callable, Dict, List, Optional

from opentelemetry.trace import Status, StatusCode

from ..telemetry import get_tracer, get_run_id

try:
    import autogen  # noqa: F401
    _AUTOGEN_AVAILABLE = True
except ImportError:
    _AUTOGEN_AVAILABLE = False


class AutoGenEvalAdapter:
    """
    Adapter for AutoGen agents.

    Wraps the agent's ``generate_reply`` method (and optionally individual
    tool-calling functions) to emit canonical aieval OTel spans around
    every message-processing cycle.
    """

    def __init__(self):
        self._tracer = get_tracer()
        # Keep track of wrapped agents so wrap_agent is idempotent
        self._wrapped_agents: Dict[int, Dict[str, Any]] = {}

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    def wrap_agent(self, agent: Any, agent_id: str, role: str = "agent") -> None:
        """
        Monkey-patch ``agent.generate_reply`` so that every invocation is
        wrapped in an ``agent.task`` span.

        Args:
            agent: An AutoGen ``ConversableAgent`` (or subclass) instance.
            agent_id: Stable identifier for this agent in evaluation traces.
            role: Semantic role label (e.g. "coder", "reviewer", "planner").
        """
        if not _AUTOGEN_AVAILABLE:
            raise ImportError(
                "AutoGen is not installed. "
                "Run: pip install pyautogen"
            )

        agent_key = id(agent)
        if agent_key in self._wrapped_agents:
            # Already wrapped - avoid double-wrapping
            return

        original_generate_reply = agent.generate_reply
        tracer = self._tracer

        def _wrapped_generate_reply(
            messages: Optional[List[Dict]] = None,
            sender: Optional[Any] = None,
            **kwargs: Any,
        ):
            # Build a concise task input from the last message in the thread
            task_input = ""
            if messages:
                last = messages[-1]
                task_input = last.get("content", json.dumps(last))
            elif sender is not None:
                task_input = f"Message from {getattr(sender, 'name', str(sender))}"

            span = tracer.start_span("agent.task")
            span.set_attribute("agent.id", agent_id)
            span.set_attribute("agent.role", role)
            span.set_attribute("task.input", str(task_input))
            span.set_attribute("run.id", get_run_id())

            import uuid
            span.set_attribute("task.id", str(uuid.uuid4()))

            ctx = __import__("opentelemetry.trace", fromlist=["use_span"]).use_span(
                span, end_on_exit=False
            )
            ctx.__enter__()
            try:
                result = original_generate_reply(
                    messages=messages, sender=sender, **kwargs
                )
                output_str = (
                    result if isinstance(result, str) else json.dumps(result)
                ) if result is not None else ""
                span.set_attribute("task.output", output_str)
                span.set_attribute("task.status", "success")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute("task.status", "failure")
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                try:
                    ctx.__exit__(None, None, None)
                except Exception:
                    pass
                span.end()

        # Bind wrapped method to agent
        import types
        agent.generate_reply = types.MethodType(_wrapped_generate_reply, agent)
        self._wrapped_agents[agent_key] = {
            "agent": agent,
            "original_generate_reply": original_generate_reply,
            "agent_id": agent_id,
            "role": role,
        }

    def wrap_tool_call(self, agent: Any, tool_name: str) -> None:
        """
        Wrap a registered tool function on an AutoGen agent so that calls
        emit ``agent.tool_call`` spans.

        AutoGen stores registered tools in ``agent._function_map``.  This
        method replaces the named entry with a tracing wrapper.

        Args:
            agent: The AutoGen agent that owns the tool.
            tool_name: The name under which the tool is registered.
        """
        if not _AUTOGEN_AVAILABLE:
            raise ImportError(
                "AutoGen is not installed. "
                "Run: pip install pyautogen"
            )

        agent_key = id(agent)
        agent_id = (
            self._wrapped_agents.get(agent_key, {}).get("agent_id")
            or getattr(agent, "name", "unknown_agent")
        )

        function_map = getattr(agent, "_function_map", None)
        if function_map is None or tool_name not in function_map:
            raise ValueError(
                f"Tool '{tool_name}' is not registered on agent "
                f"'{getattr(agent, 'name', agent)}'. "
                "Register the tool first, then call wrap_tool_call()."
            )

        original_fn: Callable = function_map[tool_name]
        tracer = self._tracer

        def _wrapped_tool(**kwargs: Any):
            span = tracer.start_span("agent.tool_call")
            span.set_attribute("agent.id", agent_id)
            span.set_attribute("tool.name", tool_name)
            span.set_attribute("tool.input", json.dumps(kwargs))

            ctx = __import__("opentelemetry.trace", fromlist=["use_span"]).use_span(
                span, end_on_exit=False
            )
            ctx.__enter__()
            try:
                result = original_fn(**kwargs)
                span.set_attribute(
                    "tool.output",
                    json.dumps(result) if not isinstance(result, str) else json.dumps({"output": result}),
                )
                span.set_attribute("tool.success", "true")
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as exc:
                span.record_exception(exc)
                span.set_attribute("tool.success", "false")
                span.set_status(Status(StatusCode.ERROR, str(exc)))
                raise
            finally:
                try:
                    ctx.__exit__(None, None, None)
                except Exception:
                    pass
                span.end()

        function_map[tool_name] = _wrapped_tool

    def unwrap_agent(self, agent: Any) -> None:
        """
        Restore the original ``generate_reply`` on a previously wrapped agent.
        Useful in tests or when recycling agents across evaluation runs.
        """
        agent_key = id(agent)
        info = self._wrapped_agents.pop(agent_key, None)
        if info is not None:
            import types
            agent.generate_reply = types.MethodType(
                info["original_generate_reply"], agent
            )
