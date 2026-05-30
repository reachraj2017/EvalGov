"""Model complexity classifier and routing engine.

Classifies a task's complexity from its input text and tool count,
then maps it to the cheapest model that should meet quality requirements.

Agents call GET /routing/recommend?task_input=...&agent_role=...
before making their LLM call. The governance service returns the
recommended model name, tier, and estimated cost per 1k tokens.

The routing table is configurable via the gov_model_routing_config
ClickHouse table. On startup the service seeds defaults if the table
is empty.
"""

from dataclasses import dataclass


# Default routing table — seeded into ClickHouse on first startup.
DEFAULT_ROUTING: dict[str, dict] = {
    "simple":   {"model": "claude-haiku-4-5-20251001", "cost_per_1m_input": 0.25,  "max_input_tokens": 8_000},
    "moderate": {"model": "claude-sonnet-4-6",          "cost_per_1m_input": 3.0,   "max_input_tokens": 16_000},
    "complex":  {"model": "claude-sonnet-4-6",          "cost_per_1m_input": 3.0,   "max_input_tokens": 32_000},
    "critical": {"model": "claude-opus-4-6",            "cost_per_1m_input": 15.0,  "max_input_tokens": 128_000},
}

# Cost of always using the most expensive model (for savings calculation).
MAX_COST_PER_1M = 15.0  # claude-opus-4-6 input price


@dataclass
class RoutingDecision:
    complexity_tier:     str    # simple | moderate | complex | critical
    model:               str
    cost_per_1m_input:   float
    max_input_tokens:    int
    input_length_chars:  int
    estimated_savings_pct: float  # vs. always using most expensive model


def classify_complexity(
    task_input: str,
    tool_count: int = 0,
    is_multi_agent: bool = False,
) -> str:
    """
    Classify task complexity from input text and execution context.

    Tiers:
      simple   — short, no tools, single-agent
      moderate — medium length or ≤2 tools
      complex  — long or ≤5 tools or multi-agent
      critical — very long or >5 tools or multi-agent + long
    """
    word_count = len(task_input.split()) if task_input else 0

    if word_count < 50 and tool_count == 0 and not is_multi_agent:
        return "simple"
    if word_count < 300 and tool_count <= 2 and not is_multi_agent:
        return "moderate"
    if word_count < 800 and tool_count <= 5:
        return "complex"
    return "critical"


def get_routing_decision(
    task_input: str,
    tool_count: int = 0,
    is_multi_agent: bool = False,
    routing_config: dict[str, dict] | None = None,
) -> RoutingDecision:
    """
    Return a RoutingDecision for a given task.

    Args:
        task_input:     The user/task message text.
        tool_count:     Number of tools the agent has available.
        is_multi_agent: Whether this is an orchestrator (spawns sub-agents).
        routing_config: Override the default routing table (from DB).
    """
    config = routing_config or DEFAULT_ROUTING
    tier   = classify_complexity(task_input, tool_count, is_multi_agent)
    cfg    = config.get(tier, DEFAULT_ROUTING["complex"])

    model             = cfg.get("model",              DEFAULT_ROUTING["complex"]["model"])
    cost_per_1m_input = float(cfg.get("cost_per_1m_input", 3.0))
    max_input_tokens  = int(cfg.get("max_input_tokens", 16_000))

    savings_pct = max(0.0, (MAX_COST_PER_1M - cost_per_1m_input) / MAX_COST_PER_1M * 100)

    return RoutingDecision(
        complexity_tier      = tier,
        model                = model,
        cost_per_1m_input    = cost_per_1m_input,
        max_input_tokens     = max_input_tokens,
        input_length_chars   = len(task_input),
        estimated_savings_pct = round(savings_pct, 1),
    )
