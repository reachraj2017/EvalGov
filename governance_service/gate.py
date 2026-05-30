"""Pre-execution HITL gate logic.

Agents call POST /gate/check before executing any sensitive action.
The gate classifies the action into a risk tier and returns a decision.

Risk tiers and their default enforcement decisions:
  low      → auto_approve  (audit-log only)
  medium   → flag          (proceed + async review)
  high     → pause         (block until human approves)
  critical → block         (hard block, dual approval required)

Additional escalation rules:
  - PII detected in the trace's recent output → escalate tier by one level
  - Agent has exceeded token budget → escalate to at least medium
"""

from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)

# Action type → base risk tier
_ACTION_TIERS: dict[str, str] = {
    # Low — read-only operations
    "read": "low", "query": "low", "search": "low", "list": "low",
    "get": "low", "fetch": "low", "view": "low", "describe": "low",
    # Medium — write operations with limited blast radius
    "write": "medium", "create": "medium", "update": "medium",
    "send_email": "medium", "send_message": "medium", "post": "medium",
    "upload": "medium", "insert": "medium",
    # High — destructive / financial / irreversible
    "delete": "high", "remove": "high", "external_write": "high",
    "financial": "high", "payment": "high", "transfer": "high",
    "modify_permissions": "high", "execute_code": "high",
    "publish": "high", "deploy": "high",
    # Critical — mass operations / admin
    "bulk_delete": "critical", "mass_update": "critical",
    "drop_table": "critical", "admin": "critical",
    "grant_admin": "critical", "revoke_access": "critical",
    # Agent-level invocation — low by default; half_open CB forces pause
    "agent_invoke": "low",
}

_TIER_ORDER = ["low", "medium", "high", "critical"]

_TIER_DECISION: dict[str, str] = {
    "low":      "auto_approve",
    "medium":   "flag",
    "high":     "pause",
    "critical": "block",
}


@dataclass
class GateResult:
    decision:          str     # auto_approve | flag | pause | block
    risk_tier:         str     # low | medium | high | critical
    base_tier:         str     # tier before escalation
    request_id:        str     # HITL queue entry (empty for auto_approve)
    requires_approval: bool
    message:           str
    escalation_reasons: list[str]


def _escalate_tier(tier: str, steps: int = 1) -> str:
    idx = _TIER_ORDER.index(tier) if tier in _TIER_ORDER else 0
    return _TIER_ORDER[min(idx + steps, len(_TIER_ORDER) - 1)]


def _phase2_enabled(db) -> bool:
    """Return True when Phase 2 enforcement is active (threshold config flag)."""
    try:
        row = db.fetch_one(
            "SELECT value FROM otel.gov_threshold_config FINAL "
            "WHERE config_key = 'enforcement.phase2_enabled'",
            {},
        )
        if row:
            return float(row.get("value", 0) or 0) >= 1.0
    except Exception:
        pass
    return False


def evaluate_gate(
    db,
    action_type: str,
    agent_role: str,
    context: dict,
    trace_id: str = "",
    run_id: str = "",
) -> GateResult:
    """
    Evaluate a pre-execution gate check.

    Args:
        db:          GovernanceDB instance.
        action_type: What the agent wants to do (e.g. "delete", "send_email").
        agent_role:  The agent's role string.
        context:     Arbitrary JSON context for the HITL queue record.
        trace_id:    Current trace ID (used to check recent PII history).
        run_id:      Current run ID.

    Returns:
        GateResult with decision and HITL queue request_id if applicable.
    """
    import json

    # 1. Base tier from action type
    action_lower = action_type.lower().strip()
    base_tier    = _ACTION_TIERS.get(action_lower, "medium")  # unknown → medium
    current_tier = base_tier
    escalation_reasons: list[str] = []

    # 1a. Phase 2 enforcement: circuit breaker check (runs before PII/budget checks)
    if _phase2_enabled(db):
        try:
            cb = db.get_circuit_breaker(agent_role)
            cb_state = str(cb.get("state", "closed") or "closed")
            if cb_state == "open":
                reason = str(cb.get("quarantine_reason", "circuit_breaker_open") or "circuit_breaker_open")
                log.warning("gate_cb_block", agent_role=agent_role, reason=reason)
                try:
                    db.save_gov_audit_log(
                        trace_id=trace_id, span_id="", run_id=run_id,
                        event_type="gate_check",
                        detail=json.dumps({
                            "action_type": action_type, "agent_role": agent_role,
                            "base_tier": base_tier, "final_tier": "critical",
                            "decision": "block", "escalations": [reason],
                        }),
                    )
                except Exception:
                    pass
                return GateResult(
                    decision="block", risk_tier="critical", base_tier=base_tier,
                    request_id="", requires_approval=False,
                    message=f"{agent_role} circuit breaker OPEN ({reason}) — action blocked",
                    escalation_reasons=[reason],
                )
            elif cb_state == "half_open":
                if action_lower == "agent_invoke":
                    # Probe: require human approval before letting agent run
                    current_tier = "high"
                    escalation_reasons.append("circuit_breaker_half_open_probe")
                else:
                    current_tier = _escalate_tier(current_tier)
                    escalation_reasons.append("circuit_breaker_half_open")
        except Exception as exc:
            log.warning("gate_cb_check_failed", agent_role=agent_role, error=str(exc))

    # 1b. Escalation: check recent quality gate block/hold decisions for this agent
    try:
        qg_blocks = db.get_recent_quality_gate_blocks(agent_role, hours=1)
        if qg_blocks:
            worst = qg_blocks[0]
            qg_action = str(worst.get("action", "flag"))
            metric     = str(worst.get("metric", ""))
            score      = float(worst.get("score", 0) or 0)
            thresh     = float(worst.get("threshold", 0) or 0)
            reason     = f"quality_gate:{metric} score={score:.3f} threshold={thresh}"
            if qg_action == "block":
                current_tier = "critical"
                escalation_reasons.append(reason)
            elif qg_action == "hold":
                current_tier = _escalate_tier(current_tier)
                escalation_reasons.append(reason)
    except Exception as exc:
        log.warning("gate_quality_gate_check_failed", agent_role=agent_role, error=str(exc))

    # 2. Escalation: check if this trace recently leaked PII
    if trace_id:
        try:
            pii_metrics = db.fetch_all(
                "SELECT value FROM otel.gov_metric_snapshots "
                "WHERE trace_id = %(tid)s AND metric = 'pii_leak_rate' AND span_id = ''",
                {"tid": trace_id},
            )
            if pii_metrics and float(pii_metrics[0].get("value", 0) or 0) > 0:
                current_tier = _escalate_tier(current_tier)
                escalation_reasons.append("PII detected in trace output")
        except Exception:
            pass

    # 3. Escalation: check budget
    try:
        from budget_tracker import check_budget
        budget_result = check_budget(db, agent_role)
        if budget_result.status == "exceeded":
            current_tier = _escalate_tier(current_tier)
            escalation_reasons.append("Token budget exceeded")
        elif budget_result.status == "warning" and current_tier == "low":
            current_tier = "medium"
            escalation_reasons.append("Token budget near limit (>80%)")
    except Exception:
        pass

    decision          = _TIER_DECISION[current_tier]
    requires_approval = decision in ("pause", "block")

    # 4. Write HITL queue entry for anything above auto_approve
    request_id = ""
    if decision != "auto_approve":
        try:
            request_id = db.save_gov_hitl_request(
                trace_id=trace_id,
                span_id="",
                run_id=run_id,
                risk_tier=current_tier,
                action_type=action_type,
                payload=json.dumps({
                    "agent_role": agent_role,
                    "context": context,
                    "escalation_reasons": escalation_reasons,
                }),
            )
        except Exception as exc:
            log.error("gate_hitl_save_failed", error=str(exc))

    # 5. Audit log
    try:
        db.save_gov_audit_log(
            trace_id=trace_id, span_id="", run_id=run_id,
            event_type="gate_check",
            detail=json.dumps({
                "action_type": action_type,
                "agent_role":  agent_role,
                "base_tier":   base_tier,
                "final_tier":  current_tier,
                "decision":    decision,
                "escalations": escalation_reasons,
            }),
        )
    except Exception:
        pass

    msg = (
        f"{action_type} by {agent_role}: {decision.upper()} "
        f"(tier: {current_tier}"
        + (f", escalated from {base_tier}" if escalation_reasons else "")
        + ")"
    )

    log.info("gate_check_complete", action=action_type, agent=agent_role,
             tier=current_tier, decision=decision)

    return GateResult(
        decision=decision, risk_tier=current_tier, base_tier=base_tier,
        request_id=request_id, requires_approval=requires_approval,
        message=msg, escalation_reasons=escalation_reasons,
    )
