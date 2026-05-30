"""Tool implementations and Anthropic schema definitions for the EvalGov Agent."""

import json
import os
from typing import Any

import httpx
import structlog

from db import AgentDB

log = structlog.get_logger()
GOV_URL = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
_db: AgentDB | None = None


def get_db() -> AgentDB:
    global _db
    if _db is None:
        _db = AgentDB()
    return _db


def _gov(path: str, params: dict | None = None) -> Any:
    try:
        r = httpx.get(f"{GOV_URL}{path}", params=params or {}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("gov_tool_error", path=path, error=str(exc))
        return {"error": str(exc)}


def _gov_put(path: str, body: dict) -> Any:
    try:
        r = httpx.put(f"{GOV_URL}{path}", json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def _gov_post(path: str, body: dict | None = None) -> Any:
    try:
        r = httpx.post(f"{GOV_URL}{path}", json=body or {}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


# ── Tool implementations ───────────────────────────────────────────────────────

def get_system_health(_: dict) -> str:
    """Aggregate snapshot of the entire system health."""
    gov = _gov("/governance/summary") or {}
    cbs = _gov("/enforcement/circuit-breakers") or []
    hitl = _gov("/hitl/queue?status=pending&limit=50") or []
    incidents = _gov("/incidents?status=open&limit=10") or []
    trust = _gov("/enforcement/trust-scores") or []
    rogue = _gov("/enforcement/rogue-assessments") or []
    open_cbs = [c for c in cbs if isinstance(c, dict) and c.get("state") == "OPEN"]
    quarantine_rec = [r for r in rogue if isinstance(r, dict) and r.get("quarantine_recommended")]
    return json.dumps({
        "pending_hitl": len(hitl),
        "open_incidents": len(incidents) if isinstance(incidents, list) else incidents.get("total", 0),
        "circuit_breakers_open": len(open_cbs),
        "agents_with_quarantine_flag": len(quarantine_rec),
        "trust_scores": [{"agent": t.get("agent_role"), "score": t.get("trust_score"), "tier": t.get("trust_tier")} for t in trust[:10]],
        "open_cbs": [{"agent": c.get("agent_role"), "state": c.get("state"), "reason": c.get("quarantine_reason", "")} for c in open_cbs],
        "governance_summary": gov,
    }, default=str)


def get_hitl_queue(inputs: dict) -> str:
    status = inputs.get("status", "")
    hours = inputs.get("hours", 24)
    path = f"/hitl/queue?limit=100&hours={hours}"
    if status:
        path += f"&status={status}"
    data = _gov(path) or []
    return json.dumps(data, default=str)


def approve_hitl(inputs: dict) -> str:
    request_id = inputs.get("request_id", "")
    reviewer = inputs.get("reviewer", "evalgov-agent")
    notes = inputs.get("notes", "Approved via EvalGov Agent")
    result = _gov_put(f"/hitl/{request_id}", {"status": "approved", "reviewer": reviewer, "notes": notes})
    return json.dumps(result, default=str)


def reject_hitl(inputs: dict) -> str:
    request_id = inputs.get("request_id", "")
    reviewer = inputs.get("reviewer", "evalgov-agent")
    notes = inputs.get("notes", "Rejected via EvalGov Agent")
    result = _gov_put(f"/hitl/{request_id}", {"status": "rejected", "reviewer": reviewer, "notes": notes})
    return json.dumps(result, default=str)


def bulk_approve_hitl(inputs: dict) -> str:
    """Approve ALL pending HITL requests in one call."""
    reviewer = inputs.get("reviewer", "operator")
    notes = inputs.get("notes", "Bulk approved via EvalGov Agent")
    data = _gov("/hitl/queue?status=pending&limit=200") or []
    requests_list = data if isinstance(data, list) else data.get("requests", []) if isinstance(data, dict) else []
    approved, errors = [], []
    for req in requests_list:
        rid = req.get("request_id", "")
        if not rid:
            continue
        result = _gov_put(f"/hitl/{rid}", {"status": "approved", "reviewer": reviewer, "notes": notes})
        if isinstance(result, dict) and result.get("error"):
            errors.append({"request_id": rid, "error": result["error"]})
        else:
            approved.append(rid)
    return json.dumps({"approved_count": len(approved), "errors": errors})


def get_circuit_breakers(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    data = _gov("/enforcement/circuit-breakers") or []
    if agent_role and isinstance(data, list):
        data = [c for c in data if c.get("agent_role") == agent_role]
    return json.dumps(data, default=str)


def reset_circuit_breaker(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    result = _gov_post(f"/enforcement/circuit-breakers/{agent_role}/reset")
    return json.dumps(result, default=str)


def quarantine_agent(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    reason = inputs.get("reason", "Manual quarantine via EvalGov Agent")
    result = _gov_post(f"/enforcement/circuit-breakers/{agent_role}/quarantine", {"reason": reason})
    return json.dumps(result, default=str)


def get_trust_scores(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    if agent_role:
        history = _gov(f"/enforcement/trust-scores/{agent_role}/history?hours={inputs.get('hours', 24)}")
        return json.dumps(history, default=str)
    data = _gov("/enforcement/trust-scores") or []
    return json.dumps(data, default=str)


def get_rogue_assessments(_: dict) -> str:
    data = _gov("/enforcement/rogue-assessments") or []
    return json.dumps(data, default=str)


def get_incidents(inputs: dict) -> str:
    status = inputs.get("status", "")
    hours = inputs.get("hours", 48)
    params: dict = {"limit": 50}
    if status:
        params["status"] = status
    if hours:
        params["hours"] = hours
    data = _gov("/incidents", params) or []
    return json.dumps(data, default=str)


def resolve_incident(inputs: dict) -> str:
    incident_id = inputs.get("incident_id", "")
    result = _gov_put(f"/incidents/{incident_id}", {"status": "resolved"})
    return json.dumps(result, default=str)


def bulk_resolve_incidents(inputs: dict) -> str:
    """Resolve ALL open incidents in one call."""
    data = _gov("/incidents", {"status": "open", "limit": 200}) or []
    incidents = data if isinstance(data, list) else []
    resolved, errors = [], []
    for inc in incidents:
        iid = inc.get("incident_id", "")
        if not iid:
            continue
        result = _gov_put(f"/incidents/{iid}", {"status": "resolved"})
        if isinstance(result, dict) and result.get("error"):
            errors.append({"incident_id": iid, "error": result["error"]})
        else:
            resolved.append(iid)
    return json.dumps({"resolved_count": len(resolved), "errors": errors})


def get_anomalies(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    data = _gov("/anomalies", {"hours": hours}) or []
    return json.dumps(data, default=str)


def get_burn_rates(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    if agent_role:
        data = _gov(f"/enforcement/burn-rates/{agent_role}")
    else:
        data = _gov("/enforcement/burn-rates")
    return json.dumps(data or {}, default=str)


def get_quality_gate_decisions(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    agent_role = inputs.get("agent_role", "")
    params: dict = {"hours": hours, "limit": 100}
    if agent_role:
        params["agent_role"] = agent_role
    data = _gov("/quality-gates/decisions", params) or {}
    return json.dumps(data, default=str)


def get_gate_audit_log(inputs: dict) -> str:
    hours = inputs.get("hours", 6)
    limit = inputs.get("limit", 50)
    data = _gov("/governance/summary") or {}
    audit_data = {
        "gate_checks": data.get("gate_checks_24h", 0),
        "gate_blocks": data.get("gate_blocks_24h", 0),
        "note": f"Detailed audit via /gate/audit endpoint. Summary covers last {hours}h."
    }
    return json.dumps(audit_data, default=str)


def get_policy_violations(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    data = _gov("/governance/summary") or {}
    return json.dumps({
        "policy_blocks_24h": data.get("policy_blocks_24h", 0),
        "policy_flags_24h": data.get("policy_flags_24h", 0),
        "note": "Policy records are audit-only; use /policies for rule definitions."
    }, default=str)


def get_recent_traces(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    hours = inputs.get("hours", 720)  # default 30 days
    limit = min(int(inputs.get("limit", 20)), 50)
    try:
        rows = get_db().get_recent_traces(agent_role, hours, limit)
        note = "Only agent.task spans are returned (spans with agent.role populated). Auto-expands to 30d if shorter window is empty."
        return json.dumps({"traces": rows, "count": len(rows), "window_hours": hours, "note": note}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_agent_performance(inputs: dict) -> str:
    hours = inputs.get("hours", 720)  # default 30 days
    try:
        scores = get_db().get_eval_scores(hours)
        return json.dumps({"eval_scores_by_metric": scores, "hours": hours, "count": len(scores)}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_cost_breakdown(inputs: dict) -> str:
    hours = inputs.get("hours", 720)
    source = inputs.get("source", "")  # production | benchmark | exploratory | "" (all)
    try:
        rows = get_db().get_cost_breakdown(hours, source=source)
        total = sum(float(r.get("total_cost_usd", 0)) for r in rows)
        total_in = sum(int(r.get("total_input_tokens", 0)) for r in rows)
        total_out = sum(int(r.get("total_output_tokens", 0)) for r in rows)
        return json.dumps({
            "by_agent": rows,
            "totals": {
                "cost_usd": round(total, 6),
                "input_tokens": total_in,
                "output_tokens": total_out,
            },
            "window_hours": hours,
            "source_filter": source or "all",
            "note": "Data from otel.prompt_evals — same source as Prompt Lab. Rates from gov_threshold_config.",
        }, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_error_rates(inputs: dict) -> str:
    hours = inputs.get("hours", 168)  # default 7 days
    try:
        rows = get_db().get_error_rate(hours)
        return json.dumps({"by_agent": rows, "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_findings(inputs: dict) -> str:
    hours = inputs.get("hours", 8760)  # default all-time (1 year) so agent sees the full backlog
    severity = inputs.get("severity", "")
    status = inputs.get("status", "active")
    try:
        rows = get_db().get_findings(hours, severity, status, limit=100)
        return json.dumps(rows, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def acknowledge_finding(inputs: dict) -> str:
    finding_id = inputs.get("finding_id", "")
    acknowledged_by = inputs.get("acknowledged_by", "operator")
    try:
        get_db().acknowledge_finding(finding_id, acknowledged_by)
        return json.dumps({"status": "acknowledged", "finding_id": finding_id})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def resolve_finding(inputs: dict) -> str:
    finding_id = inputs.get("finding_id", "")
    try:
        get_db().resolve_finding(finding_id)
        return json.dumps({"status": "resolved", "finding_id": finding_id})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def bulk_resolve_findings(inputs: dict) -> str:
    """Resolve ALL active (and optionally acknowledged) findings in one call."""
    acknowledged_by = inputs.get("acknowledged_by", "operator")
    try:
        db = get_db()
        rows = db.get_findings(hours=8760, status="", limit=500)  # all time, all statuses
        active = [r for r in rows if r.get("status") in ("active", "acknowledged")]
        resolved = []
        errors = []
        for r in active:
            fid = r.get("finding_id", "")
            if not fid:
                continue
            try:
                db.resolve_finding(fid)
                resolved.append(fid)
            except Exception as exc:
                errors.append({"finding_id": fid, "error": str(exc)})
        return json.dumps({"resolved_count": len(resolved), "errors": errors})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def bulk_acknowledge_findings(inputs: dict) -> str:
    """Acknowledge ALL active findings in one call."""
    acknowledged_by = inputs.get("acknowledged_by", "operator")
    try:
        db = get_db()
        rows = db.get_findings(hours=8760, status="active", limit=500)
        acknowledged = []
        errors = []
        for r in rows:
            fid = r.get("finding_id", "")
            if not fid:
                continue
            try:
                db.acknowledge_finding(fid, acknowledged_by)
                acknowledged.append(fid)
            except Exception as exc:
                errors.append({"finding_id": fid, "error": str(exc)})
        return json.dumps({"acknowledged_count": len(acknowledged), "errors": errors})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_compliance_report(_: dict) -> str:
    data = _gov("/compliance/report") or {}
    return json.dumps(data, default=str)


def get_reliability_summary(_: dict) -> str:
    data = _gov("/reliability/summary") or []
    return json.dumps(data, default=str)


# ── Eval content tools ─────────────────────────────────────────────────────────

def get_prompt_detail(inputs: dict) -> str:
    trace_id = inputs.get("trace_id", "")
    if not trace_id:
        return json.dumps({"error": "trace_id is required"})
    try:
        rows = get_db().get_prompt_detail(trace_id)
        return json.dumps({"prompts": rows, "count": len(rows)}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def search_prompts(inputs: dict) -> str:
    agent_name = inputs.get("agent_name", "")
    source = inputs.get("source", "")
    conversation_id = inputs.get("conversation_id", "")
    hours = inputs.get("hours", 720)
    limit = inputs.get("limit", 10)
    try:
        rows = get_db().search_prompts(agent_name, source, conversation_id, hours, limit)
        return json.dumps({"prompts": rows, "count": len(rows), "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_eval_runs(inputs: dict) -> str:
    suite = inputs.get("suite", "")
    hours = inputs.get("hours", 720)
    limit = inputs.get("limit", 20)
    try:
        rows = get_db().get_eval_runs(suite, hours, limit)
        return json.dumps({"runs": rows, "count": len(rows), "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_benchmarks(inputs: dict) -> str:
    suite = inputs.get("suite", "")
    difficulty = inputs.get("difficulty", "")
    limit = inputs.get("limit", 50)
    try:
        rows = get_db().get_benchmarks(suite, difficulty, limit)
        return json.dumps({"benchmarks": rows, "count": len(rows)}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_eval_scores_detail(inputs: dict) -> str:
    trace_id = inputs.get("trace_id", "")
    run_id = inputs.get("run_id", "")
    metric = inputs.get("metric", "")
    limit = inputs.get("limit", 20)
    try:
        rows = get_db().get_eval_scores_detail(trace_id, run_id, metric, limit)
        return json.dumps({"scores": rows, "count": len(rows)}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ── Governance config / state tools ───────────────────────────────────────────

def get_safety_events(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    agent_role = inputs.get("agent_role", "")
    params: dict = {"hours": hours, "limit": 100}
    if agent_role:
        params["agent_role"] = agent_role
    data = _gov("/safety/events", params) or []
    return json.dumps(data, default=str)


def get_thresholds(inputs: dict) -> str:
    category = inputs.get("category", "")
    data = _gov("/thresholds") or []
    if category and isinstance(data, list):
        data = [t for t in data if t.get("category") == category]
    return json.dumps(data, default=str)


def get_agent_budgets(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    if agent_role:
        data = _gov(f"/budget/{agent_role}")
    else:
        data = _gov("/budget")
    return json.dumps(data or {}, default=str)


def get_version_pins(_: dict) -> str:
    data = _gov("/lifecycle/versions") or []
    return json.dumps(data, default=str)


def get_lifecycle_changes(inputs: dict) -> str:
    hours = inputs.get("hours", 168)
    agent_role = inputs.get("agent_role", "")
    params: dict = {"hours": hours, "limit": 100}
    if agent_role:
        params["agent_role"] = agent_role
    data = _gov("/lifecycle/changes", params) or []
    return json.dumps(data, default=str)


def get_compliance_scorecard(inputs: dict) -> str:
    framework = inputs.get("framework", "")
    params = {"framework": framework} if framework else {}
    data = _gov("/regulatory/scorecard", params) or {}
    return json.dumps(data, default=str)


def get_risk_register(inputs: dict) -> str:
    status = inputs.get("status", "")
    params = {"status": status} if status else {}
    data = _gov("/regulatory/risk-register", params) or []
    return json.dumps(data, default=str)


def get_model_registry(_: dict) -> str:
    data = _gov("/model-registry") or []
    return json.dumps(data, default=str)


def get_policy_decisions(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    agent_role = inputs.get("agent_role", "")
    decision = inputs.get("decision", "")
    limit = inputs.get("limit", 50)
    try:
        rows = get_db().get_policy_decisions(hours, agent_role, decision, limit)
        return json.dumps({"decisions": rows, "count": len(rows), "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_routing_decisions(inputs: dict) -> str:
    hours = inputs.get("hours", 168)
    agent_role = inputs.get("agent_role", "")
    limit = inputs.get("limit", 50)
    try:
        rows = get_db().get_routing_decisions(hours, agent_role, limit)
        return json.dumps({"decisions": rows, "count": len(rows), "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ── Tool dispatcher ────────────────────────────────────────────────────────────

TOOL_MAP = {
    # System & governance enforcement
    "get_system_health": get_system_health,
    "get_hitl_queue": get_hitl_queue,
    "approve_hitl": approve_hitl,
    "reject_hitl": reject_hitl,
    "bulk_approve_hitl": bulk_approve_hitl,
    "get_circuit_breakers": get_circuit_breakers,
    "reset_circuit_breaker": reset_circuit_breaker,
    "quarantine_agent": quarantine_agent,
    "get_trust_scores": get_trust_scores,
    "get_rogue_assessments": get_rogue_assessments,
    "get_incidents": get_incidents,
    "resolve_incident": resolve_incident,
    "bulk_resolve_incidents": bulk_resolve_incidents,
    "get_anomalies": get_anomalies,
    "get_burn_rates": get_burn_rates,
    "get_quality_gate_decisions": get_quality_gate_decisions,
    "get_gate_audit_log": get_gate_audit_log,
    "get_policy_violations": get_policy_violations,
    "get_safety_events": get_safety_events,
    "get_policy_decisions": get_policy_decisions,
    "get_routing_decisions": get_routing_decisions,
    # Eval & agent activity
    "get_recent_traces": get_recent_traces,
    "get_agent_performance": get_agent_performance,
    "get_cost_breakdown": get_cost_breakdown,
    "get_error_rates": get_error_rates,
    "get_prompt_detail": get_prompt_detail,
    "search_prompts": search_prompts,
    "get_eval_runs": get_eval_runs,
    "get_benchmarks": get_benchmarks,
    "get_eval_scores_detail": get_eval_scores_detail,
    # Config & compliance
    "get_thresholds": get_thresholds,
    "get_agent_budgets": get_agent_budgets,
    "get_version_pins": get_version_pins,
    "get_lifecycle_changes": get_lifecycle_changes,
    "get_compliance_scorecard": get_compliance_scorecard,
    "get_risk_register": get_risk_register,
    "get_model_registry": get_model_registry,
    "get_compliance_report": get_compliance_report,
    "get_reliability_summary": get_reliability_summary,
    # Findings
    "get_findings": get_findings,
    "acknowledge_finding": acknowledge_finding,
    "resolve_finding": resolve_finding,
    "bulk_resolve_findings": bulk_resolve_findings,
    "bulk_acknowledge_findings": bulk_acknowledge_findings,
}


def execute_tool(name: str, inputs: dict) -> str:
    fn = TOOL_MAP.get(name)
    if not fn:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        return fn(inputs)
    except Exception as exc:
        log.error("tool_execution_error", tool=name, error=str(exc))
        return json.dumps({"error": str(exc)})


# ── Anthropic tool schemas ─────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "get_system_health",
        "description": "Get a complete real-time health snapshot of the entire AI governance system: pending HITL requests, open circuit breakers, open incidents, quarantine flags, and trust scores for all agents.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_hitl_queue",
        "description": "List HITL (Human-in-the-Loop) approval requests. Agents are blocked waiting for approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending", "approved", "rejected", ""], "description": "Filter by status. Empty = all."},
                "hours": {"type": "integer", "description": "Look-back window in hours (default 24). Pending requests always shown."},
            },
        },
    },
    {
        "name": "approve_hitl",
        "description": "Approve a pending HITL request. The blocked agent will immediately resume its action.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "The request_id from the HITL queue"},
                "reviewer": {"type": "string", "description": "Name or identifier of the approver"},
                "notes": {"type": "string", "description": "Optional approval notes"},
            },
            "required": ["request_id"],
        },
    },
    {
        "name": "reject_hitl",
        "description": "Reject a pending HITL request. The agent will receive a governance block and cannot proceed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "reviewer": {"type": "string"},
                "notes": {"type": "string", "description": "Reason for rejection (required by policy)"},
            },
            "required": ["request_id", "notes"],
        },
    },
    {
        "name": "bulk_approve_hitl",
        "description": "Approve ALL pending HITL requests in one operation. Use when the user asks to approve or clear all pending HITL requests.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reviewer": {"type": "string", "description": "Who is approving (default: operator)"},
                "notes": {"type": "string", "description": "Approval notes applied to all requests"},
            },
        },
    },
    {
        "name": "get_circuit_breakers",
        "description": "Get circuit breaker states for all agents (CLOSED=healthy, OPEN=blocked, HALF_OPEN=recovering). OPEN means all actions are blocked.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Filter to a specific agent. Empty = all agents."},
            },
        },
    },
    {
        "name": "reset_circuit_breaker",
        "description": "Manually reset (close) an agent's circuit breaker, allowing it to resume operations.",
        "input_schema": {
            "type": "object",
            "properties": {"agent_role": {"type": "string"}},
            "required": ["agent_role"],
        },
    },
    {
        "name": "quarantine_agent",
        "description": "Manually quarantine an agent (set CB to OPEN with quarantine flag). All actions blocked until manual reset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string"},
                "reason": {"type": "string", "description": "Reason for quarantine"},
            },
            "required": ["agent_role", "reason"],
        },
    },
    {
        "name": "get_trust_scores",
        "description": "Get trust scores for all agents (composite score from identity, behavior, compliance, network signals). Lower score = less trustworthy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Specific agent for history. Empty = latest all agents."},
                "hours": {"type": "integer", "description": "History window in hours (only used when agent_role is specified)"},
            },
        },
    },
    {
        "name": "get_rogue_assessments",
        "description": "Get rogue agent detection assessments: frequency_score, entropy_score, capability_score, composite_score, and quarantine_recommended flag.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_incidents",
        "description": "List governance incidents (open or resolved). Incidents are automatically triggered by safety, identity, reliability, or behavior violations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "resolved", "contained", ""], "description": "Filter by status"},
                "hours": {"type": "integer", "description": "Look-back window in hours"},
            },
        },
    },
    {
        "name": "resolve_incident",
        "description": "Mark a single incident as resolved by incident_id.",
        "input_schema": {
            "type": "object",
            "properties": {"incident_id": {"type": "string"}},
            "required": ["incident_id"],
        },
    },
    {
        "name": "bulk_resolve_incidents",
        "description": "Resolve ALL open incidents in one operation. Use when the user asks to resolve or close all incidents.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_anomalies",
        "description": "Get recent anomaly detection events (statistical outliers in agent behavior vs. established baselines).",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "description": "Look-back window in hours (default 24)"}},
        },
    },
    {
        "name": "get_burn_rates",
        "description": "Get error budget burn rates for agents. High burn rate = SLO exhaustion approaching.",
        "input_schema": {
            "type": "object",
            "properties": {"agent_role": {"type": "string", "description": "Specific agent or empty for all"}},
        },
    },
    {
        "name": "get_quality_gate_decisions",
        "description": "Get content quality gate decisions (flag/hold/block) triggered by the quality checker for output quality issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window"},
                "agent_role": {"type": "string", "description": "Filter by agent"},
            },
        },
    },
    {
        "name": "get_policy_violations",
        "description": "Get summary of policy engine violations (policy flags and blocks recorded in the audit log).",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer"}},
        },
    },
    {
        "name": "get_recent_traces",
        "description": "Get recent agent task spans from OTel traces. Returns agent.task spans with agent_role, duration, status, and timestamp. Default window is 30 days — auto-expands if shorter window returns empty. Agent traces may be days or weeks old if the demo hasn't run recently.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Filter by agent role (e.g. 'searcher', 'summarizer'). Empty = all agents."},
                "hours": {"type": "integer", "description": "Look-back window in hours. Default: 720 (30 days). Use 720 or higher if you suspect data is old."},
                "limit": {"type": "integer", "description": "Max traces to return (max 50, default 20)"},
            },
        },
    },
    {
        "name": "get_agent_performance",
        "description": "Get eval scores by metric (faithfulness, hallucination, relevance, coherence, etc.) aggregated over the given window. Default 30 days.",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "description": "Look-back window in hours (default 720 = 30 days)"}},
        },
    },
    {
        "name": "get_cost_breakdown",
        "description": (
            "Get token usage and USD cost broken down by agent — same data source as Prompt Lab (otel.prompt_evals). "
            "Rates come from governance config, not hardcoded. Filter by source to see only production costs, "
            "only eval/benchmark costs, or all combined. Default 30-day window, auto-expands if empty."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 720 = 30 days)"},
                "source": {
                    "type": "string",
                    "enum": ["production", "benchmark", "exploratory", ""],
                    "description": "Filter by trace source. 'production' = live agent calls only; 'benchmark' = eval runs; '' = all combined.",
                },
            },
        },
    },
    {
        "name": "get_error_rates",
        "description": "Get error rates per agent role on agent.task spans over the given time window. Default 7 days.",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "description": "Look-back window in hours (default 168 = 7 days)"}},
        },
    },
    {
        "name": "get_findings",
        "description": "Get proactive monitor findings (anomalies, RCA analyses, recommendations) generated by the background monitor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window"},
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", ""]},
                "status": {"type": "string", "enum": ["active", "acknowledged", "resolved", ""]},
            },
        },
    },
    {
        "name": "acknowledge_finding",
        "description": "Acknowledge a single proactive monitor finding by finding_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "acknowledged_by": {"type": "string"},
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "resolve_finding",
        "description": "Resolve (close) a single proactive monitor finding by finding_id. Use this to mark a finding as fixed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "bulk_resolve_findings",
        "description": "Resolve ALL active and acknowledged findings in one operation. Use when the user asks to clear, dismiss, or resolve all findings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "acknowledged_by": {"type": "string", "description": "Who is resolving (default: operator)"},
            },
        },
    },
    {
        "name": "bulk_acknowledge_findings",
        "description": "Acknowledge ALL active findings in one operation. Use when the user asks to acknowledge all findings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "acknowledged_by": {"type": "string", "description": "Who is acknowledging (default: operator)"},
            },
        },
    },
    {
        "name": "get_compliance_report",
        "description": "Generate and return a compliance summary report covering all governance categories.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_reliability_summary",
        "description": "Get reliability metrics summary per agent (uptime, error rate, SLO compliance).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    # ── Eval content ──────────────────────────────────────────────────────────
    {
        "name": "get_prompt_detail",
        "description": (
            "Get the full prompt text, response text, token counts, cost_usd, eval scores, and conversation_id for a specific trace ID. "
            "cost_usd is computed from token counts using the governance threshold rates. "
            "conversation_id groups all turns belonging to the same user session — use it to see which conversation a trace belongs to. "
            "Use this to answer 'what was the actual user prompt?', 'what did the agent respond?', 'what did this trace cost?', or 'which conversation is this from?'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string", "description": "The trace ID to look up in otel.prompt_evals"},
            },
            "required": ["trace_id"],
        },
    },
    {
        "name": "search_prompts",
        "description": (
            "Search recent prompt/response pairs from otel.prompt_evals (same as Prompt Lab). "
            "Returns prompt_text, response_text, agent, model, tokens, scores, source, and conversation_id. "
            "conversation_id groups all prompts from the same user session — filter by it to isolate a full conversation. "
            "Use this to browse recent prompts, find what users asked, review agent responses, or inspect a specific user session."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Filter by agent name (e.g. 'orchestrator', 'searcher')"},
                "source": {
                    "type": "string",
                    "enum": ["production", "benchmark", "exploratory", ""],
                    "description": "Filter by source. Empty = all.",
                },
                "conversation_id": {"type": "string", "description": "Filter by conversation/session ID to retrieve all prompts from a specific user session."},
                "hours": {"type": "integer", "description": "Look-back window in hours (default 720 = 30 days)"},
                "limit": {"type": "integer", "description": "Max results (default 10, max 50)"},
            },
        },
    },
    {
        "name": "get_eval_runs",
        "description": "List evaluation test runs from otel.eval_runs. Shows run name, suite (unit/integration/collaboration/production), agent version, baseline flag, and timestamp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "suite": {"type": "string", "enum": ["unit", "integration", "collaboration", "production", ""], "description": "Filter by test suite"},
                "hours": {"type": "integer", "description": "Look-back window in hours (default 720 = 30 days)"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
        },
    },
    {
        "name": "get_benchmarks",
        "description": "List benchmark test case definitions including task_input (the test question/prompt), expected_output, rubric, suite, and difficulty. Use to see what test cases exist in the system.",
        "input_schema": {
            "type": "object",
            "properties": {
                "suite": {"type": "string", "enum": ["unit", "integration", "collaboration", "production", ""], "description": "Filter by suite"},
                "difficulty": {"type": "string", "enum": ["easy", "medium", "hard", ""], "description": "Filter by difficulty"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
        },
    },
    {
        "name": "get_eval_scores_detail",
        "description": "Get eval scores with full reasoning text for a specific trace or run. Shows metric, score, and the evaluator's justification for the score. Use to explain why an agent scored well or poorly.",
        "input_schema": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string", "description": "Filter by specific trace"},
                "run_id": {"type": "string", "description": "Filter by eval run"},
                "metric": {"type": "string", "description": "Filter by metric name (e.g. 'faithfulness', 'relevance')"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
        },
    },
    # ── Safety & policy ───────────────────────────────────────────────────────
    {
        "name": "get_safety_events",
        "description": "Get safety rule violation events including the matched pattern and matched_text that triggered the violation. Use to investigate what content triggered safety rules.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 24)"},
                "agent_role": {"type": "string", "description": "Filter by agent role"},
            },
        },
    },
    {
        "name": "get_policy_decisions",
        "description": "Get policy engine verdicts (warn/block/pass) from gov_policy_decisions. Shows which metric triggered the decision, the observed value vs threshold, and the message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 24)"},
                "agent_role": {"type": "string", "description": "Filter by agent role"},
                "decision": {"type": "string", "enum": ["warn", "block", "pass", ""], "description": "Filter by decision type"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
        },
    },
    {
        "name": "get_routing_decisions",
        "description": "Get model routing decisions: which complexity tier was chosen, which model was selected, and estimated cost savings. Use to understand how requests are being routed.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 168 = 7 days)"},
                "agent_role": {"type": "string", "description": "Filter by agent role"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
        },
    },
    # ── Config & compliance ───────────────────────────────────────────────────
    {
        "name": "get_thresholds",
        "description": "Get all configurable governance thresholds (token budgets, cost limits, SLO targets, safety scores, etc.) with current values and units. Filter by category to narrow results.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category (e.g. 'budget', 'slo', 'safety', 'trust')"},
            },
        },
    },
    {
        "name": "get_agent_budgets",
        "description": "Get token budget configuration and current daily usage per agent. Shows daily_token_limit, cost_usd_limit, tokens used today, and budget utilization percentage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Specific agent to look up. Empty = all agents."},
            },
        },
    },
    {
        "name": "get_version_pins",
        "description": "Get model version pin configuration per agent — which model version is pinned, deployment mode, and whether the pin is active.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_lifecycle_changes",
        "description": "Get the configuration change log — what was changed, old vs new values, who changed it, and when. Use to audit recent configuration modifications.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 168 = 7 days)"},
                "agent_role": {"type": "string", "description": "Filter by agent"},
            },
        },
    },
    {
        "name": "get_compliance_scorecard",
        "description": "Get compliance scorecard scores by regulatory framework (SOC2, GDPR, HIPAA, etc.) showing controls passing/failing per agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "framework": {"type": "string", "description": "Filter by framework name (e.g. 'SOC2', 'GDPR'). Empty = all."},
            },
        },
    },
    {
        "name": "get_risk_register",
        "description": "Get the AI governance risk register — all tracked risks with likelihood, impact, owner, mitigation status, and category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "mitigated", "accepted", "closed", ""], "description": "Filter by risk status"},
            },
        },
    },
    {
        "name": "get_model_registry",
        "description": "Get the approved model registry — which models are whitelisted, their provider, license type, commercial use flag, and sector restrictions.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]
