"""DB-backed policy store with UI-editable rules.

Loads policy rules from otel.gov_policies (ReplacingMergeTree).
Falls back to hardcoded defaults if the table is empty.
Supports global rules and scoped rules per agent_role or service_name.
"""

import uuid
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

# Default policies inserted on first startup
_SEED_POLICIES = [
    {"metric": "pii_leak_rate",            "threshold": 0.0,  "direction": "gt",  "decision": "warn",  "description": "PII detected in any output"},
    {"metric": "pii_leak_rate",            "threshold": 0.10, "direction": "gt",  "decision": "block", "description": "PII leak rate exceeds 10%"},
    {"metric": "pii_input_rate",           "threshold": 0.0,  "direction": "gt",  "decision": "warn",  "description": "PII detected in prompt input"},
    {"metric": "prompt_snapshot_coverage", "threshold": 1.0,  "direction": "lt",  "decision": "warn",  "description": "Not all spans carry prompt snapshot hash"},
    {"metric": "prompt_snapshot_coverage", "threshold": 0.50, "direction": "lt",  "decision": "block", "description": "Prompt snapshot coverage below 50%"},
    {"metric": "budget_utilization",       "threshold": 0.80, "direction": "gt",  "decision": "warn",  "description": "Agent token budget above 80%"},
    {"metric": "budget_utilization",       "threshold": 1.00, "direction": "gte", "decision": "block", "description": "Agent token budget exceeded"},
]


@dataclass
class PolicyRule:
    policy_id:   str
    metric:      str
    threshold:   float
    direction:   str   # gt | lt | gte | lte
    decision:    str   # warn | block
    scope:       str   # global | agent_role | service
    scope_value: str
    enabled:     bool
    description: str


def load_policies(db, agent_role: str = "", service: str = "") -> list[PolicyRule]:
    """Load enabled policies from DB, filtered for this agent/service context."""
    try:
        rows = db.fetch_all(
            "SELECT policy_id, metric, threshold, direction, decision, "
            "scope, scope_value, enabled, description "
            "FROM otel.gov_policies FINAL WHERE enabled = 1 "
            "ORDER BY decision DESC, metric ASC"
        )
    except Exception as exc:
        log.warning("policy_store_load_failed", error=str(exc))
        return _hardcoded_defaults()

    if not rows:
        return _seed_and_return(db)

    rules = []
    for r in rows:
        scope       = str(r.get("scope", "global"))
        scope_value = str(r.get("scope_value", "") or "")
        if scope == "global":
            include = True
        elif scope == "agent_role" and agent_role and scope_value == agent_role:
            include = True
        elif scope == "service" and service and scope_value == service:
            include = True
        else:
            include = False

        if include:
            rules.append(PolicyRule(
                policy_id=str(r.get("policy_id", "")),
                metric=str(r.get("metric", "")),
                threshold=float(r.get("threshold", 0) or 0),
                direction=str(r.get("direction", "gt")),
                decision=str(r.get("decision", "warn")),
                scope=scope,
                scope_value=scope_value,
                enabled=bool(r.get("enabled", 1)),
                description=str(r.get("description", "") or ""),
            ))
    return rules


def get_all_policies(db) -> list[dict]:
    """Return all policy rows (including disabled) for the UI editor."""
    try:
        return db.fetch_all(
            "SELECT policy_id, metric, threshold, direction, decision, "
            "scope, scope_value, enabled, description, created_at, updated_at "
            "FROM otel.gov_policies FINAL ORDER BY metric ASC, decision DESC"
        )
    except Exception as exc:
        log.warning("get_all_policies_failed", error=str(exc))
        return []


def save_policy(
    db,
    metric: str,
    threshold: float,
    direction: str,
    decision: str,
    scope: str = "global",
    scope_value: str = "",
    enabled: int = 1,
    description: str = "",
    policy_id: str = "",
) -> str:
    """Upsert a policy rule. Returns the policy_id."""
    pid = policy_id or str(uuid.uuid4())
    db.execute(
        "INSERT INTO otel.gov_policies "
        "(policy_id, metric, threshold, direction, decision, "
        "scope, scope_value, enabled, description) VALUES",
        [(pid, metric, float(threshold), direction, decision,
          scope, scope_value, int(enabled), description)],
    )
    return pid


def delete_policy(db, policy_id: str) -> None:
    """Soft-delete by disabling the policy (ReplacingMergeTree keeps latest row)."""
    db.execute(
        "ALTER TABLE otel.gov_policies UPDATE enabled = 0 "
        "WHERE policy_id = %(pid)s",
        {"pid": policy_id},
    )


def backtest_policy(
    db,
    metric: str,
    threshold: float,
    direction: str,
    decision: str,
    hours: int = 168,
) -> dict:
    """
    Simulate how many traces a new policy would have affected over the last N hours.

    Returns: {total_traces, would_trigger, trigger_pct, sample_values}
    """
    try:
        op = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}.get(direction, ">")
        rows = db.fetch_all(
            f"SELECT trace_id, value FROM otel.gov_metric_snapshots "
            f"WHERE metric = %(metric)s AND span_id = '' "
            f"  AND ts >= now() - INTERVAL {int(hours)} HOUR",
            {"metric": metric},
        )
        total    = len(rows)
        triggers = [r for r in rows if _compare(float(r["value"] or 0), float(threshold), op)]
        samples  = [round(float(r["value"] or 0), 4) for r in triggers[:5]]
        return {
            "total_traces":  total,
            "would_trigger": len(triggers),
            "trigger_pct":   round(len(triggers) / total * 100, 1) if total > 0 else 0.0,
            "sample_values": samples,
            "hours_tested":  hours,
        }
    except Exception as exc:
        log.warning("backtest_failed", metric=metric, error=str(exc))
        return {"total_traces": 0, "would_trigger": 0, "trigger_pct": 0.0,
                "sample_values": [], "hours_tested": hours}


def _compare(value: float, threshold: float, op: str) -> bool:
    return (op == ">" and value > threshold) or \
           (op == "<" and value < threshold) or \
           (op == ">=" and value >= threshold) or \
           (op == "<=" and value <= threshold)


def _seed_and_return(db) -> list[PolicyRule]:
    try:
        for p in _SEED_POLICIES:
            db.execute(
                "INSERT INTO otel.gov_policies "
                "(metric, threshold, direction, decision, description) VALUES",
                [(p["metric"], float(p["threshold"]), p["direction"],
                  p["decision"], p["description"])],
            )
        log.info("gov_policies_seeded", count=len(_SEED_POLICIES))
    except Exception as exc:
        log.warning("gov_policies_seed_failed", error=str(exc))
    return _hardcoded_defaults()


def _hardcoded_defaults() -> list[PolicyRule]:
    return [PolicyRule("", p["metric"], float(p["threshold"]), p["direction"],
                       p["decision"], "global", "", True, p["description"])
            for p in _SEED_POLICIES]
