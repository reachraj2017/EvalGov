"""LiteLLM-backed agent with tool use loop for EvalGov intelligence."""

import json
import os
from typing import Any

import structlog

from tools import TOOL_SCHEMAS, execute_tool

log = structlog.get_logger()

SYSTEM_PROMPT = """You are the EvalGov Intelligence Agent for an AI governance and observability platform.
You have direct, real-time access to all metrics, incidents, alerts, traces, HITL queues, circuit breakers,
eval scores, costs, and governance signals for the AI agents deployed in this system.

Your role:
- Answer questions with real data — always call tools before answering, never guess numbers
- Proactively correlate signals (e.g., if a CB is open, also check trust scores and recent incidents for that agent)
- Give specific, actionable answers: exact numbers, agent names, timestamps, and recommended next steps
- When something is wrong, explain the root cause concisely and recommend one clear action
- Be direct and concise — operators need fast answers, not essays

IMPORTANT — handling empty results:
- If get_recent_traces, get_cost_breakdown, get_agent_performance, or get_error_rates return empty,
  ALWAYS retry immediately with a wider window (hours=720 for 30 days) before concluding there is no data.
  Agent traces may be days or weeks old if the system hasn't had recent activity.
- Always state the actual time window you used in your answer (e.g., "Over the last 30 days...").
- Never report "no data" after only checking a 24h window. Try 720h first if the first call returns empty.
- get_recent_traces only returns agent.task spans (those with agent.role set). It does NOT return
  eval-runner spans (eval.score, eval.pipeline) — those are evaluation infrastructure, not agent invocations.

When the user asks "what's wrong" or "status" or similar broad questions:
1. Call get_system_health() first for a full snapshot
2. If issues exist, drill into the specific problem (CBs, HITL, incidents, rogue agents)
3. Summarize findings and recommend the most important action

When the user asks about traces, costs, or agent activity:
- Default to 30-day window (hours=720) for all trace/cost/performance queries
- Present results as: agent name, invocation count, total tokens, total cost in USD
- Cost data comes from otel.prompt_evals (same as Prompt Lab). Use source="production" to show only
  live agent costs, source="benchmark" for eval runs, or omit source for the combined total.
- Numbers will match what the user sees in Prompt Lab when the same source filter is applied.

When the user asks about actual prompt text, user inputs, or agent responses:
- Use get_prompt_detail(trace_id) to retrieve the full prompt_text and response_text for a specific trace.
- Use search_prompts() to browse recent prompts — filter by agent_name or source.

When the user asks about eval test runs or benchmarks:
- Use get_eval_runs() to list test runs with suite/baseline info.
- Use get_benchmarks() to see test case definitions (task_input, expected_output, rubric).
- Use get_eval_scores_detail(trace_id or run_id) to show scores with full reasoning text.

When the user asks about configuration, limits, or policies:
- Use get_thresholds() for all configurable governance parameters.
- Use get_agent_budgets() for token limits and today's usage.
- Use get_version_pins() for model version locks.
- Use get_lifecycle_changes() for the config change audit log.

When the user asks about compliance, risk, or model approvals:
- Use get_compliance_scorecard() for framework scores (SOC2, GDPR, etc.).
- Use get_risk_register() for open risk items.
- Use get_model_registry() for the approved model list.

When the user asks about safety violations or policy decisions:
- Use get_safety_events() to see what content triggered safety rules (includes matched_text).
- Use get_policy_decisions() for policy engine verdicts with decision=block for hard blocks.
- Use get_routing_decisions() to see model routing choices and cost savings.

You can take actions (approve/reject HITL, reset CBs, quarantine agents, resolve incidents) — always
confirm what action you took and what the effect will be."""

_DEFAULT_AGENT_MODEL = "anthropic/claude-sonnet-4-6"


def _agent_model() -> str:
    return os.getenv("AGENT_MODEL", _DEFAULT_AGENT_MODEL)


def _litellm_kwargs(model: str) -> dict:
    """Return extra kwargs needed for the given model provider."""
    kwargs: dict = {}
    if "ollama" in model.lower():
        kwargs["api_base"] = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        kwargs["extra_body"] = {"think": False}
    return kwargs


def _to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool schemas (input_schema) to OpenAI format (parameters)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in anthropic_tools
    ]


_OPENAI_TOOLS = _to_openai_tools(TOOL_SCHEMAS)


def chat(user_message: str, history: list[dict]) -> tuple[str, list[dict]]:
    """
    Run one chat turn with the agent.

    Args:
        user_message: the user's latest message
        history: prior conversation turns [{role, content}]

    Returns:
        (response_text, tool_calls_used)
        where tool_calls_used = [{name, inputs, result}]
    """
    import litellm

    messages: list[dict] = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + list(history)
        + [{"role": "user", "content": user_message}]
    )
    tool_calls_used: list[dict] = []
    model = _agent_model()

    for _ in range(10):  # max 10 tool-call rounds
        response = litellm.completion(
            model=model,
            max_tokens=4096,
            tools=_OPENAI_TOOLS,
            messages=messages,
            **_litellm_kwargs(model),
        )

        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []

        if tool_calls or choice.finish_reason == "tool_calls":

            # Append assistant message with tool_calls for next round
            messages.append({
                "role": "assistant",
                "content": choice.message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            # Execute each tool and append results
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                result = execute_tool(tc.function.name, args)
                tool_calls_used.append({
                    "name": tc.function.name,
                    "inputs": args,
                    "result": result[:500],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
                log.info("tool_called", tool=tc.function.name)

        else:
            return choice.message.content or "", tool_calls_used

    return "I reached the tool call limit. Please ask a more specific question.", tool_calls_used


def generate_rca(signal_summary: str) -> dict:
    """
    Call the configured model to generate a structured RCA + recommendation for a detected anomaly.
    Returns {severity, summary, rca, recommendation}.
    """
    import litellm

    model = _agent_model()
    response = litellm.completion(
        model=model,
        max_tokens=1024,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an expert AI governance analyst. You will receive a structured signal summary "
                    "describing an anomaly detected in a live AI agent system. "
                    "Respond ONLY with a JSON object (no markdown) with these exact fields:\n"
                    '  "severity": one of "critical"|"high"|"medium"|"low"\n'
                    '  "summary": 2-sentence plain-English description of the problem\n'
                    '  "rca": 2-sentence root cause analysis\n'
                    '  "recommendation": one specific, actionable next step for the operator\n'
                    "Be concise. Do not include any text outside the JSON object."
                ),
            },
            {"role": "user", "content": signal_summary},
        ],
        **_litellm_kwargs(model),
    )
    text = response.choices[0].message.content or "{}"
    # Strip markdown code fences (```json ... ```) that some models add
    if text.strip().startswith("```"):
        text = "\n".join(
            line for line in text.splitlines()
            if not line.strip().startswith("```")
        ).strip()
    try:
        return json.loads(text)
    except Exception:
        return {
            "severity": "medium",
            "summary": text[:200],
            "rca": "Could not parse structured RCA.",
            "recommendation": "Review the signal data manually.",
        }
