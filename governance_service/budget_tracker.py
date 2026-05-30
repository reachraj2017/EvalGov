"""Token budget tracking and enforcement.

Reads per-agent daily token usage from the prompt_evals table
(which the eval pipeline populates) and compares against configured
budgets in gov_token_budgets.

Budget check result statuses:
  ok        — usage < 80% of daily limit
  warning   — usage 80–100% of daily limit
  exceeded  — usage > 100% of daily limit
  no_budget — no budget configured for this agent
"""

from dataclasses import dataclass


@dataclass
class BudgetCheckResult:
    agent_role:      str
    status:          str        # ok | warning | exceeded | no_budget
    within_budget:   bool
    tokens_used:     int
    daily_limit:     int        # 0 = no limit
    utilization:     float      # 0.0–∞  (>1.0 means over budget)
    cost_usd_today:  float


def check_budget(db, agent_role: str, thresholds: dict = None) -> BudgetCheckResult:
    """
    Check whether an agent is within its daily token budget.

    Args:
        db:         GovernanceDB instance.
        agent_role: The agent's role string (must match gov_token_budgets).

    Returns:
        BudgetCheckResult with status and utilization details.
    """
    t = thresholds or {}
    _warning_band  = float(t.get("budget.warning_utilization",  0.80))
    _exceeded_band = float(t.get("budget.exceeded_utilization", 1.0))
    budget = db.get_token_budget(agent_role)
    usage  = db.get_token_usage_today(agent_role)

    tokens_used    = int(usage.get("total_tokens", 0) or 0)
    cost_usd_today = float(usage.get("total_cost_usd", 0.0) or 0.0)

    if not budget or not budget.get("enabled", 1):
        return BudgetCheckResult(
            agent_role=agent_role, status="no_budget", within_budget=True,
            tokens_used=tokens_used, daily_limit=0,
            utilization=0.0, cost_usd_today=cost_usd_today,
        )

    daily_limit = int(budget.get("daily_token_limit", 0) or 0)

    if daily_limit <= 0:
        return BudgetCheckResult(
            agent_role=agent_role, status="no_budget", within_budget=True,
            tokens_used=tokens_used, daily_limit=0,
            utilization=0.0, cost_usd_today=cost_usd_today,
        )

    utilization = tokens_used / daily_limit

    if utilization >= _exceeded_band:
        status = "exceeded"
    elif utilization >= _warning_band:
        status = "warning"
    else:
        status = "ok"

    return BudgetCheckResult(
        agent_role=agent_role, status=status, within_budget=(utilization < _exceeded_band),
        tokens_used=tokens_used, daily_limit=daily_limit,
        utilization=round(utilization, 4), cost_usd_today=cost_usd_today,
    )


def check_all_budgets(db) -> list[BudgetCheckResult]:
    """Return budget check results for every agent that has usage today."""
    agents = db.get_agents_with_usage_today()
    results = []
    for agent_role in agents:
        results.append(check_budget(db, agent_role))
    return results
