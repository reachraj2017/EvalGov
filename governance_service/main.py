"""Governance Service — FastAPI application (Phase 1 + 2 + 3 + 4).

Endpoints:
  GET  /health
  POST /gate/check                      pre-execution HITL gate
  GET  /hitl/queue                      review queue
  GET  /hitl/{request_id}/status        agent polling endpoint
  PUT  /hitl/{request_id}               approve / reject
  GET  /routing/recommend               model routing
  GET  /budget/{agent_role}             single-agent budget
  GET  /budget                          all agents budget
  GET  /drift/{template_id}             prompt drift history
  POST /drift/{template_id}/promote     promote baseline

  — Phase 3 —
  GET  /policies                        list policy rules
  POST /policies                        create policy rule
  PUT  /policies/{policy_id}            update rule
  DELETE /policies/{policy_id}          delete (disable) rule
  POST /policies/backtest               simulate rule against history

  GET  /anomalies                       recent anomaly events
  POST /baselines/recompute             recompute behavioral baselines

  GET  /compliance/report               generate + return summary report
  GET  /compliance/reports              list saved reports
  GET  /compliance/reports/{report_id}  fetch full report JSON
  GET  /compliance/lineage/{trace_id}   data lineage for a trace

  GET  /agent-keys                      list API keys
  POST /agent-keys                      create new key (returns plaintext once)
  DELETE /agent-keys/{key_id}           revoke key

  GET  /webhooks                        list webhook configs
  POST /webhooks                        create webhook
  PUT  /webhooks/{webhook_id}           update webhook
  DELETE /webhooks/{webhook_id}         remove webhook

  — Phase 4 —
  GET  /safety/events                   safety events log
  GET  /safety/summary                  safety events summary
  GET  /safety/rules                    list safety rules
  POST /safety/rules                    create safety rule
  DELETE /safety/rules/{rule_id}        disable safety rule

  GET  /identity/events                 identity / access events log
  GET  /identity/summary                identity events summary
  GET  /supply-chain                    supply-chain artifact registry
  POST /supply-chain                    add supply-chain artifact
  GET  /tool-whitelist                  tool whitelist entries
  POST /tool-whitelist                  add tool whitelist entry

  GET  /reliability                     reliability metrics
  GET  /reliability/summary             reliability summary by agent
  GET  /slo                             SLO configs
  POST /slo                             upsert SLO config

  GET  /incidents                       incident list
  GET  /incidents/summary               incident KPIs (MTTD/MTTC/MTTR)
  PUT  /incidents/{incident_id}         update incident status

  GET  /behavior/events                 behavior events log
  GET  /behavior/summary                behavior metrics summary
  GET  /persona                         persona configs
  POST /persona                         upsert persona config

  GET  /lifecycle/changes               change-log entries
  GET  /lifecycle/versions              version-pin entries
  POST /lifecycle/versions              upsert version pin

  GET  /regulatory/scorecard            compliance scorecard (all or one framework)
  GET  /regulatory/scorecards/history   saved scorecard history
  GET  /regulatory/scope                regulatory scope entries
  POST /regulatory/scope                add regulatory scope entry
  GET  /regulatory/risk-register        risk register
  POST /regulatory/risk-register        create risk item
  PUT  /regulatory/risk-register/{id}   update risk item

  GET  /model-registry                  model registry entries
  POST /model-registry                  add model registry entry

  GET  /governance/full-summary         expanded governance summary

  — Enforcement (Phase 1) —
  GET  /enforcement/trust-scores                      latest trust score per agent
  GET  /enforcement/trust-scores/{agent_role}/history trust score history
  GET  /enforcement/rogue-assessments                 latest rogue assessment per agent
  GET  /enforcement/rogue-assessments/{role}/history  rogue assessment history
  GET  /enforcement/burn-rates                        burn rates for all agents
  GET  /enforcement/burn-rates/{agent_role}           burn rates for one agent
  GET  /enforcement/circuit-breakers                  all circuit breaker states
  POST /enforcement/circuit-breakers/{role}/reset     manual reset (close)
  POST /enforcement/circuit-breakers/{role}/quarantine manual quarantine (open)
  POST /enforcement/run-cycle/{agent_role}            run full enforcement cycle now
"""

import asyncio
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Optional

import structlog
from fastapi import FastAPI, HTTPException, Query, Request
from pydantic import BaseModel

from anomaly_detector import recompute_baselines
from auth import generate_key, resolve_agent_key

# ── Threshold seed defaults ───────────────────────────────────────────────────
_THRESHOLD_DEFAULTS: list[tuple] = [
    # (config_key, display_name, category, value, unit, description)
    ("safety.injection_confidence",            "Injection Confidence",            "safety",     0.9,   "ratio",   "Min confidence to flag prompt injection"),
    ("safety.jailbreak_confidence",            "Jailbreak Confidence",            "safety",     0.85,  "ratio",   "Min confidence to flag jailbreak attempt"),
    ("safety.toxicity_score",                  "Toxicity Score Cutoff",           "safety",     0.5,   "ratio",   "Eval score threshold for toxic output"),
    ("safety.toxic_keyword_confidence",        "Toxic Keyword Confidence",        "safety",     0.7,   "ratio",   "Confidence for keyword-based toxicity fallback"),
    ("safety.bias_score",                      "Bias Score Cutoff",               "safety",     0.5,   "ratio",   "Eval score threshold for bias detection"),
    ("safety.bias_keyword_confidence",         "Bias Keyword Confidence",         "safety",     0.6,   "ratio",   "Confidence for keyword-based bias fallback"),
    ("identity.session_ttl_seconds",           "Session Token TTL",               "identity",   3600,  "seconds", "Max session token age before flagging"),
    ("identity.least_privilege_ratio",         "Least Privilege Ratio",           "identity",   0.5,   "ratio",   ">X of unused tools = over-provisioned flag"),
    ("behavior.consistency_decrement",         "Consistency Decrement",           "behavior",   0.2,   "ratio",   "Score decrement per unique output"),
    ("behavior.min_output_length",             "Min Output Length",               "behavior",   100,   "chars",   "Min chars to assess persona adherence"),
    ("behavior.ood_zscore_multiplier",         "OOD Z-score Multiplier",          "behavior",   3.0,   "zscore",  "mean + N*stddev for OOD detection"),
    ("anomaly.zscore_medium",                  "Anomaly Z-score Medium",          "anomaly",    2.0,   "zscore",  "Z-score threshold for medium severity"),
    ("anomaly.zscore_high",                    "Anomaly Z-score High",            "anomaly",    3.0,   "zscore",  "Z-score threshold for high severity"),
    ("anomaly.zscore_critical",                "Anomaly Z-score Critical",        "anomaly",    4.0,   "zscore",  "Z-score threshold for critical severity"),
    ("anomaly.min_samples",                    "Anomaly Min Samples",             "anomaly",    5,     "count",   "Min samples for valid baseline"),
    ("budget.warning_utilization",             "Budget Warning Threshold",        "budget",     0.8,   "ratio",   "Utilization pct to trigger warning status"),
    ("budget.exceeded_utilization",            "Budget Exceeded Threshold",       "budget",     1.0,   "ratio",   "Utilization pct to trigger exceeded status"),
    ("budget.input_token_cost_per_1m",         "Input Token Cost (per 1M)",       "budget",     0.15,  "usd",     "Cost in USD per 1M input/prompt tokens (default: gpt-4o-mini)"),
    ("budget.output_token_cost_per_1m",        "Output Token Cost (per 1M)",      "budget",     0.60,  "usd",     "Cost in USD per 1M output/completion tokens (default: gpt-4o-mini)"),
    ("incident.error_budget_breach_multiplier","Error Budget Breach Multiplier",  "incident",   2.0,   "ratio",   "Error budget consumed > N times → P2 incident"),
    ("incident.dedup_window_days",             "Incident Dedup Window",           "incident",   30,    "days",    "Dedup open incidents within N days"),
    ("regulatory.pii_pass_threshold",          "PII Pass Threshold",              "regulatory", 0.01,  "ratio",   "PII leak rate <= N = GDPR/HIPAA pass"),
    ("regulatory.audit_coverage_pass",         "Audit Coverage Pass",             "regulatory", 0.95,  "ratio",   "Audit coverage >= N = pass"),
    ("regulatory.audit_coverage_partial",      "Audit Coverage Partial",          "regulatory", 0.5,   "ratio",   "Audit coverage >= N = partial"),
    ("regulatory.prompt_snapshot_coverage",    "Prompt Snapshot Coverage",        "regulatory", 0.95,  "ratio",   "Snapshot coverage >= N = pass"),
    ("regulatory.availability_slo_pass",       "Availability SLO Pass",           "regulatory", 0.99,  "ratio",   "Availability >= N = SOC2 A1 pass"),
    ("regulatory.compliance_partial_ratio",    "Compliance Partial Ratio",        "regulatory", 0.5,   "ratio",   "Relative lower bound for partial status"),
    ("regulatory.compliance_partial_multiplier","Compliance Partial Multiplier",  "regulatory", 2.0,   "ratio",   "Relative upper bound for partial status (lte)"),
    ("enforcement.phase2_enabled",             "Pre-Execution Enforcement Enabled", "enforcement", 0,    "bool",    "1 = enable pre-execution enforcement (circuit breaker gate + trust gating); 0 = observe only"),
    ("enforcement.quality_gates_enabled",      "Quality Gates Enabled",           "enforcement", 0,    "bool",    "1 = enable content quality gate checks against eval scores; 0 = observe only"),
    ("cb.failure_threshold",                   "CB Failure Threshold",            "enforcement", 5,    "count",   "Number of high/critical incidents in last 1h before circuit breaker opens"),
    ("cb.recovery_minutes",                    "CB Recovery Minutes",             "enforcement", 30,   "minutes", "Minutes an OPEN circuit breaker waits before transitioning to HALF_OPEN for probe"),
    ("quality_gate.hold_to_block_threshold",   "QG Hold-to-Block Threshold",      "enforcement", 5,    "count",   "Confirmed/expired holds before auto-block fires and CB failure is recorded"),
    ("quality_gate.hold_review_timeout_seconds","QG Hold Review Timeout",          "enforcement", 300,  "seconds", "Seconds before an unreviewed hold HITL entry is auto-expired and counted toward block threshold"),
]


def seed_threshold_config(db) -> None:
    """Seed default threshold values if they don't already exist."""
    try:
        existing = {r["config_key"] for r in db.get_threshold_config_all()}
        for (key, display_name, category, value, unit, description) in _THRESHOLD_DEFAULTS:
            if key not in existing:
                db.save_threshold(
                    config_key=key, value=float(value),
                    display_name=display_name, category=category,
                    unit=unit, description=description, updated_by="system",
                )
        log.info("threshold_config_seeded", count=len(_THRESHOLD_DEFAULTS))
    except Exception as exc:
        log.warning("threshold_config_seed_failed", error=str(exc))
from budget_tracker import check_all_budgets, check_budget
from compliance_reporter import generate_summary, get_trace_lineage
from db import GovernanceDB
from drift_detector import promote_to_baseline
from gate import evaluate_gate
from model_router import DEFAULT_ROUTING, get_routing_decision
from policy_store import (
    backtest_policy, delete_policy, get_all_policies,
    load_policies, save_policy,
)
from runner import GovernanceRunner
from watcher import run_watcher
from safety_guard import run_safety_checks, seed_safety_rules
from enforcement_engine import (
    compute_trust_score, assess_rogue_risk, compute_burn_rates,
    evaluate_circuit_breaker, run_enforcement_cycle,
)
from identity_guard import run_identity_checks, _normalize_model_name
from reliability_tracker import check_reliability, seed_slo_config
from behavior_analyzer import run_behavior_checks
from lifecycle_tracker import run_lifecycle_checks
from incident_manager import auto_detect_incidents, compute_mttd, compute_mttc, compute_mttr, compute_recurrence_rate
from regulatory_manager import (
    compute_compliance_scorecard, get_risk_register, save_risk_item,
    get_regulatory_scope, save_regulatory_scope,
    get_model_registry, save_model_registry,
)

structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger(__name__)

_db:            Optional[GovernanceDB]     = None
_runner:        Optional[GovernanceRunner] = None
_watcher_task:  Optional[asyncio.Task]    = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db, _runner, _watcher_task

    _db     = GovernanceDB()
    try:
        _db.ensure_tables()
    except Exception as exc:
        log.warning("ensure_tables_failed", error=str(exc))

    _runner = GovernanceRunner(db=_db)

    try:
        _db.seed_routing_config(DEFAULT_ROUTING)
    except Exception as exc:
        log.warning("routing_config_seed_failed", error=str(exc))

    # Seed default policies if table is empty
    try:
        load_policies(_db)
    except Exception as exc:
        log.warning("policy_seed_failed", error=str(exc))
    try:
        seed_safety_rules(_db)
    except Exception as exc:
        log.warning("safety_rules_seed_failed", error=str(exc))
    try:
        seed_slo_config(_db)
    except Exception as exc:
        log.warning("slo_config_seed_failed", error=str(exc))
    try:
        seed_threshold_config(_db)
    except Exception as exc:
        log.warning("threshold_config_seed_failed", error=str(exc))

    # Recompute anomaly baselines on startup
    try:
        recompute_baselines(_db)
    except Exception as exc:
        log.warning("baseline_recompute_failed", error=str(exc))

    _watcher_task = asyncio.create_task(run_watcher(_db, _runner))

    log.info("governance_service_started")
    yield

    if _watcher_task:
        _watcher_task.cancel()
        try:
            await _watcher_task
        except asyncio.CancelledError:
            pass
    log.info("governance_service_stopped")


app = FastAPI(
    title="Governance Service",
    description="AI agent governance — HITL gate, policy engine, compliance",
    version="4.0.0",
    lifespan=lifespan,
)


def _db_() -> GovernanceDB:
    if _db is None:
        raise RuntimeError("GovernanceDB not initialised")
    return _db


# ── Request / Response models ─────────────────────────────────────────────────

class GateCheckRequest(BaseModel):
    action_type: str
    agent_role:  str
    context:     dict = {}
    trace_id:    str  = ""
    run_id:      str  = ""


class HitlDecisionRequest(BaseModel):
    status:   str        # approved | rejected
    reviewer: str = ""
    notes:    str = ""


class PromoteBaselineRequest(BaseModel):
    snapshot_hash: str


class PolicyCreateRequest(BaseModel):
    metric:      str
    threshold:   float
    direction:   str   # gt | lt | gte | lte
    decision:    str   # warn | block
    scope:       str   = "global"
    scope_value: str   = ""
    enabled:     int   = 1
    description: str   = ""


class PolicyBacktestRequest(BaseModel):
    metric:    str
    threshold: float
    direction: str
    decision:  str
    hours:     int = 168


class CreateKeyRequest(BaseModel):
    agent_role: str


class WebhookRequest(BaseModel):
    name:    str
    url:     str
    events:  list[str] = []
    enabled: int       = 1
    secret:  str       = ""


class ComplianceReportRequest(BaseModel):
    from_ts:       str = ""    # ISO datetime; defaults to 7 days ago
    to_ts:         str = ""    # ISO datetime; defaults to now
    generated_by:  str = "api"


class SloConfigRequest(BaseModel):
    agent_role: str
    target_availability: float = 0.999
    error_budget_pct: float = 0.001
    slo_window_days: int = 30
    p95_target_ms: float = 3000.0
    p99_target_ms: float = 10000.0


class SafetyRuleRequest(BaseModel):
    rule_type: str  # injection | jailbreak | toxic | bias
    pattern: str
    severity: str = "high"
    description: str = ""
    enabled: int = 1


class ToolWhitelistRequest(BaseModel):
    agent_role: str
    tool_name: str
    allowed: int = 1


class PersonaConfigRequest(BaseModel):
    agent_role: str
    authorized_topics: list = []
    persona_description: str = ""


class VersionPinRequest(BaseModel):
    agent_role: str
    model_name: str
    pinned_version: str
    is_pinned: int = 1
    deployment_mode: str = "production"


class RiskItemRequest(BaseModel):
    title: str
    category: str = ""
    likelihood: int = 3
    impact: int = 3
    owner: str = ""
    mitigation: str = ""
    status: str = "open"
    risk_id: str = ""


class RegulatoryScope(BaseModel):
    agent_role: str = ""
    framework: str
    sector: str = ""
    classification: str = ""
    notes: str = ""


class ModelRegistryRequest(BaseModel):
    model_name: str
    model_version: str = ""
    provider: str = ""
    license_type: str = "proprietary"
    commercial_ok: int = 1
    dpa_signed: int = 0
    baa_signed: int = 0
    sectors_allowed: list = []
    notes: str = ""


class SupplyChainRequest(BaseModel):
    artifact_type: str
    artifact_name: str
    expected_hash: str = ""
    verified: int = 1


class IncidentUpdateRequest(BaseModel):
    status: str  # open | contained | resolved
    root_cause: str = ""


class ToggleKeyRequest(BaseModel):
    enabled: int


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "governance-service", "version": "4.0.0"}


# ── Gate ─────────────────────────────────────────────────────────────────────

@app.post("/gate/check")
async def gate_check(body: GateCheckRequest, request: Request) -> dict:
    db = _db_()
    await resolve_agent_key(request, db)
    try:
        r = evaluate_gate(db=db, action_type=body.action_type,
                          agent_role=body.agent_role, context=body.context,
                          trace_id=body.trace_id, run_id=body.run_id)
        return {"decision": r.decision, "risk_tier": r.risk_tier,
                "base_tier": r.base_tier, "request_id": r.request_id,
                "requires_approval": r.requires_approval, "message": r.message,
                "escalation_reasons": r.escalation_reasons}
    except Exception as exc:
        log.error("gate_check_error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


# ── HITL queue ────────────────────────────────────────────────────────────────

@app.get("/hitl/queue")
async def hitl_queue(
    status: str = Query(default=""),
    limit: int = Query(default=200),
    hours: int = Query(default=0),
) -> list:
    return _db_().get_gov_hitl_queue(status=status, limit=limit, hours=hours)


@app.get("/hitl/{request_id}/status")
async def hitl_status(request_id: str) -> dict:
    row = _db_().get_hitl_request_status(request_id)
    if not row:
        raise HTTPException(status_code=404, detail="Request not found")
    return {"request_id": request_id, "status": row.get("status", "pending"),
            "reviewer": row.get("reviewer", ""), "notes": row.get("notes", "")}


@app.put("/hitl/{request_id}")
async def hitl_decide(request_id: str, body: HitlDecisionRequest) -> dict:
    if body.status not in ("approved", "rejected"):
        raise HTTPException(status_code=400, detail="status must be approved or rejected")
    import json as _json
    from quality_gate_checker import _check_hold_threshold
    db = _db_()

    # Fetch original entry before updating — need action_type and payload
    original = db.fetch_one(
        "SELECT action_type, payload FROM otel.gov_hitl_queue FINAL WHERE request_id = %(rid)s",
        {"rid": request_id},
    ) or {}
    action_type = str(original.get("action_type", ""))
    try:
        payload = _json.loads(original.get("payload", "{}") or "{}")
    except Exception:
        payload = {}
    agent_role  = payload.get("agent_role", "")
    decision_id = payload.get("quality_gate_decision_id", "")

    db.update_gov_hitl_decision(request_id, body.status, body.reviewer, body.notes)

    if body.status == "approved":
        if agent_role:
            # Clear quality gate block/hold signals — agent is released
            db.clear_quality_gate_blocks(agent_role)
            if action_type == "quality_gate_block":
                # Operator overrode the block — decrement CB failure count (strict: only on approve)
                db.decrement_quality_gate_cb_failure(agent_role)

    elif body.status == "rejected":
        if action_type == "quality_gate_hold" and agent_role:
            # Operator confirmed this hold is a real quality failure
            # Mark the underlying decision as rejected so hold counter includes it
            if decision_id:
                db.mark_quality_gate_decision(decision_id, "rejected")
            # Check if hold threshold is now reached → auto-block + CB failure
            try:
                _check_hold_threshold(db, agent_role, trigger="reject")
            except Exception as exc:
                log.warning("hold_threshold_check_failed", agent_role=agent_role, error=str(exc))

    return {"request_id": request_id, "status": body.status}


@app.post("/hitl/bulk-expire-quality-gates")
async def bulk_expire_quality_gates(reviewer: str = Query(default="operator")) -> dict:
    """Expire all pending quality_gate_hold and quality_gate_block HITL entries.

    Cleanup-only — does not trigger CB threshold escalation.
    """
    count = _db_().bulk_expire_quality_gate_hitl(reviewer=reviewer)
    return {"expired": count}


# ── Routing ───────────────────────────────────────────────────────────────────

@app.get("/routing/recommend")
async def routing_recommend(
    task_input:     str  = Query(default=""),
    tool_count:     int  = Query(default=0),
    is_multi_agent: bool = Query(default=False),
) -> dict:
    db  = _db_()
    cfg = db.get_routing_config() or None
    rd  = get_routing_decision(task_input=task_input, tool_count=tool_count,
                                is_multi_agent=is_multi_agent, routing_config=cfg)
    return {"complexity_tier": rd.complexity_tier, "model": rd.model,
            "cost_per_1m_input": rd.cost_per_1m_input,
            "max_input_tokens": rd.max_input_tokens,
            "input_length_chars": rd.input_length_chars,
            "estimated_savings_pct": rd.estimated_savings_pct}


# ── Budget ────────────────────────────────────────────────────────────────────

@app.get("/budget/{agent_role}")
async def budget_single(agent_role: str) -> dict:
    r = check_budget(_db_(), agent_role)
    return {"agent_role": r.agent_role, "status": r.status,
            "within_budget": r.within_budget, "tokens_used": r.tokens_used,
            "daily_limit": r.daily_limit, "utilization": r.utilization,
            "cost_usd_today": r.cost_usd_today}


@app.get("/budget")
async def budget_all() -> list:
    return [{"agent_role": r.agent_role, "status": r.status,
             "within_budget": r.within_budget, "tokens_used": r.tokens_used,
             "daily_limit": r.daily_limit, "utilization": r.utilization}
            for r in check_all_budgets(_db_())]


# ── Prompt drift ──────────────────────────────────────────────────────────────

@app.get("/drift/{template_id}")
async def drift_history(template_id: str) -> list:
    return _db_().get_prompt_drift_history(template_id)


@app.post("/drift/{template_id}/promote")
async def drift_promote(template_id: str, body: PromoteBaselineRequest) -> dict:
    promote_to_baseline(_db_(), template_id, body.snapshot_hash)
    return {"template_id": template_id, "new_baseline": body.snapshot_hash}


# ── Policies (Phase 3) ────────────────────────────────────────────────────────

@app.get("/policies")
async def list_policies() -> list:
    return get_all_policies(_db_())


@app.post("/policies", status_code=201)
async def create_policy(body: PolicyCreateRequest) -> dict:
    db  = _db_()
    pid = save_policy(db, metric=body.metric, threshold=body.threshold,
                      direction=body.direction, decision=body.decision,
                      scope=body.scope, scope_value=body.scope_value,
                      enabled=body.enabled, description=body.description)
    return {"policy_id": pid, "status": "created"}


@app.put("/policies/{policy_id}")
async def update_policy(policy_id: str, body: PolicyCreateRequest) -> dict:
    db  = _db_()
    pid = save_policy(db, metric=body.metric, threshold=body.threshold,
                      direction=body.direction, decision=body.decision,
                      scope=body.scope, scope_value=body.scope_value,
                      enabled=body.enabled, description=body.description,
                      policy_id=policy_id)
    return {"policy_id": pid, "status": "updated"}


@app.delete("/policies/{policy_id}")
async def remove_policy(policy_id: str) -> dict:
    delete_policy(_db_(), policy_id)
    return {"policy_id": policy_id, "status": "disabled"}


@app.post("/policies/backtest")
async def policy_backtest(body: PolicyBacktestRequest) -> dict:
    return backtest_policy(_db_(), metric=body.metric, threshold=body.threshold,
                           direction=body.direction, decision=body.decision,
                           hours=body.hours)


# ── Anomaly detection (Phase 3) ───────────────────────────────────────────────

@app.get("/anomalies")
async def list_anomalies(hours: int = Query(default=24), limit: int = Query(default=200)) -> list:
    return _db_().get_anomaly_events(hours=hours, limit=limit)


@app.post("/baselines/recompute")
async def trigger_recompute(window_days: int = Query(default=7)) -> dict:
    try:
        recompute_baselines(_db_(), window_days=window_days)
        return {"status": "ok", "window_days": window_days}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Compliance (Phase 3) ──────────────────────────────────────────────────────

@app.post("/compliance/report")
async def create_compliance_report(body: ComplianceReportRequest) -> dict:
    db = _db_()
    now = datetime.utcnow()
    try:
        from_ts = datetime.fromisoformat(body.from_ts) if body.from_ts else now - timedelta(days=7)
        to_ts   = datetime.fromisoformat(body.to_ts)   if body.to_ts   else now
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid datetime: {exc}")

    from dataclasses import asdict
    report = generate_summary(db, from_ts, to_ts, body.generated_by)
    return asdict(report)


@app.get("/compliance/reports")
async def list_compliance_reports(limit: int = Query(default=20)) -> list:
    return _db_().get_compliance_reports(limit=limit)


@app.get("/compliance/reports/{report_id}")
async def get_compliance_report(report_id: str) -> dict:
    import json as _json
    row = _db_().get_compliance_report(report_id)
    if not row:
        raise HTTPException(status_code=404, detail="Report not found")
    try:
        return _json.loads(str(row.get("payload", "{}")))
    except Exception:
        return dict(row)


@app.get("/compliance/lineage/{trace_id}")
async def trace_lineage(trace_id: str) -> dict:
    return get_trace_lineage(_db_(), trace_id)


# ── Agent keys (Phase 3) ──────────────────────────────────────────────────────

@app.get("/agent-keys")
async def list_keys() -> list:
    return _db_().list_agent_keys()


@app.post("/agent-keys", status_code=201)
async def create_key(body: CreateKeyRequest) -> dict:
    db         = _db_()
    full_key, prefix, key_hash = generate_key()
    key_id     = str(uuid.uuid4())
    db.save_agent_key(key_id, body.agent_role, key_hash, prefix)
    log.info("agent_key_created", agent_role=body.agent_role, prefix=prefix)
    return {"key_id": key_id, "agent_role": body.agent_role,
            "key": full_key,   # plaintext returned once, never stored
            "key_prefix": prefix,
            "warning": "Save this key now — it will not be shown again."}


@app.delete("/agent-keys/{key_id}")
async def revoke_key(key_id: str) -> dict:
    _db_().revoke_agent_key(key_id)
    return {"key_id": key_id, "status": "revoked"}


@app.put("/agent-keys/{key_id}")
async def toggle_key(key_id: str, body: ToggleKeyRequest) -> dict:
    _db_().toggle_agent_key(key_id, body.enabled)
    return {"key_id": key_id, "enabled": body.enabled}


# ── Webhooks (Phase 3) ────────────────────────────────────────────────────────

@app.get("/webhooks")
async def list_webhooks() -> list:
    return _db_().list_webhooks()


@app.post("/webhooks", status_code=201)
async def create_webhook(body: WebhookRequest) -> dict:
    wid = str(uuid.uuid4())
    _db_().save_webhook(wid, body.name, body.url, body.events, body.enabled, body.secret)
    return {"webhook_id": wid, "status": "created"}


@app.put("/webhooks/{webhook_id}")
async def update_webhook(webhook_id: str, body: WebhookRequest) -> dict:
    _db_().save_webhook(webhook_id, body.name, body.url, body.events,
                        body.enabled, body.secret)
    return {"webhook_id": webhook_id, "status": "updated"}


@app.delete("/webhooks/{webhook_id}")
async def remove_webhook(webhook_id: str) -> dict:
    _db_().delete_webhook(webhook_id)
    return {"webhook_id": webhook_id, "status": "disabled"}


# ── Summary (for UI polling) ──────────────────────────────────────────────────

@app.get("/governance/summary")
async def governance_summary(hours: int = Query(default=24)) -> dict:
    db = _db_()
    try:
        r1 = db.fetch_all(
            f"SELECT count(DISTINCT trace_id) AS traces_scanned, "
            f"countIf(metric='pii_in_output' AND value>0) AS pii_output_hits, "
            f"avgIf(value, metric='prompt_snapshot_coverage') AS snapshot_coverage, "
            f"avgIf(value, metric='pii_leak_rate') AS avg_pii_leak_rate "
            f"FROM otel.gov_metric_snapshots "
            f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR"
        )
        r2 = db.fetch_all(
            f"SELECT countIf(decision='block') AS total_blocks, "
            f"countIf(decision='warn') AS total_warnings, "
            f"countIf(decision='pass') AS total_passes "
            f"FROM otel.gov_policy_decisions "
            f"WHERE ts >= now() - INTERVAL {int(hours)} HOUR"
        )
        summary: dict = {}
        if r1: summary.update(r1[0])
        if r2: summary.update(r2[0])
        return summary
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ── Category 4: Safety ──────────────────────────────────────────────────

@app.get("/safety/events")
async def list_safety_events(trace_id: str = "", hours: int = Query(default=24), limit: int = 100):
    db = _db_()
    conds = [f"ts >= now() - INTERVAL %(h)s HOUR"]
    params: dict = {"h": hours, "limit": limit}
    if trace_id:
        conds.append("trace_id=%(t)s")
        params["t"] = trace_id
    where = "WHERE " + " AND ".join(conds)
    return db.fetch_all(
        f"SELECT event_id, trace_id, agent_role, event_type, detected, pattern_name, confidence, detail, ts "
        f"FROM otel.gov_safety_events {where} ORDER BY ts DESC LIMIT %(limit)s",
        params,
    ) or []

@app.get("/safety/summary")
async def safety_summary(hours: int = 24):
    db = _db_()
    base = db.get_safety_summary(hours=hours) or {}
    return {
        "total_events":     int(base.get("total_count",    0) or 0),
        "injection_count":  int(base.get("injection_count", 0) or 0),
        "jailbreak_count":  int(base.get("jailbreak_count", 0) or 0),
        "toxic_bias_count": int(base.get("toxic_bias_count", 0) or 0),
    }

@app.get("/safety/rules")
async def list_safety_rules():
    return _db_().get_safety_rules()

@app.post("/safety/rules")
async def create_safety_rule(req: SafetyRuleRequest):
    import re as _re
    # Strip JS-style regex delimiters e.g. /pattern/flags → pattern
    pattern = req.pattern.strip()
    if pattern.startswith("/"):
        last_slash = pattern.rfind("/")
        if last_slash > 0:
            pattern = pattern[1:last_slash]
    # Validate the pattern compiles
    try:
        _re.compile(pattern, _re.IGNORECASE)
    except _re.error as e:
        from fastapi import HTTPException
        raise HTTPException(status_code=422, detail=f"Invalid regex pattern: {e}")
    rid = _db_().save_safety_rule(req.rule_type, pattern, req.severity, req.description, req.enabled)
    return {"rule_id": rid}

@app.delete("/safety/rules/{rule_id}")
async def delete_safety_rule(rule_id: str):
    _db_().execute("ALTER TABLE otel.gov_safety_rules UPDATE enabled=0 WHERE rule_id=%(rid)s", {"rid": rule_id})
    return {"deleted": rule_id}

# ── Category 2: Identity & Access ───────────────────────────────────────

@app.get("/identity/events")
async def list_identity_events(trace_id: str = "", hours: int = Query(default=24), limit: int = 100):
    db = _db_()
    conds = [f"ts >= now() - INTERVAL %(h)s HOUR"]
    params: dict = {"h": hours, "limit": limit}
    if trace_id:
        conds.append("trace_id=%(t)s")
        params["t"] = trace_id
    where = "WHERE " + " AND ".join(conds)
    return db.fetch_all(
        f"SELECT event_id, trace_id, agent_role, event_type, severity, detail, ts "
        f"FROM otel.gov_identity_events {where} ORDER BY ts DESC LIMIT %(limit)s",
        params,
    ) or []

@app.get("/identity/summary")
async def identity_summary(hours: int = 24):
    db = _db_()
    try:
        rows = db.fetch_all(
            "SELECT event_type, count() AS cnt FROM otel.gov_identity_events "
            "WHERE ts >= now() - INTERVAL %(h)s HOUR GROUP BY event_type",
            {"h": hours},
        )
        type_map = {r["event_type"]: int(r.get("cnt", 0)) for r in (rows or [])}
    except Exception:
        type_map = {}
    total = sum(type_map.values())
    return {
        "total_events":        total,
        "auth_failures":       type_map.get("auth_failure",       0),
        "unregistered_agents": type_map.get("unregistered_agent", 0),
        "tool_violations":     type_map.get("tool_violation",     0),
        "credential_leaks":    type_map.get("credential_leak",    0),
        "events_by_type":      rows or [],
    }

@app.get("/supply-chain")
async def list_supply_chain():
    return _db_().get_supply_chain_registry()

@app.post("/supply-chain")
async def add_supply_chain(req: SupplyChainRequest):
    name = _normalize_model_name(req.artifact_name) if req.artifact_type == "model" else req.artifact_name
    eid = _db_().save_supply_chain_entry(req.artifact_type, name, req.expected_hash, req.verified)
    return {"artifact_id": eid, "artifact_name": name}

@app.put("/supply-chain/{artifact_id}")
async def update_supply_chain(artifact_id: str, req: SupplyChainRequest):
    name = _normalize_model_name(req.artifact_name) if req.artifact_type == "model" else req.artifact_name
    _db_().update_supply_chain_entry(
        artifact_id, req.artifact_type, name,
        req.expected_hash, req.verified,
    )
    return {"artifact_id": artifact_id, "artifact_name": name, "status": "updated"}

@app.delete("/supply-chain/{artifact_id}")
async def delete_supply_chain(artifact_id: str):
    _db_().delete_supply_chain_entry(artifact_id)
    return {"artifact_id": artifact_id, "status": "deleted"}

@app.get("/tool-whitelist")
async def list_tool_whitelist(agent_role: str = ""):
    return _db_().get_tool_whitelist(agent_role=agent_role)

@app.post("/tool-whitelist")
async def add_tool_whitelist(req: ToolWhitelistRequest):
    _db_().save_tool_whitelist_entry(req.agent_role, req.tool_name, req.allowed)
    return {"ok": True}

@app.put("/tool-whitelist/{entry_id}")
async def update_tool_whitelist(entry_id: str, req: ToolWhitelistRequest):
    _db_().update_tool_whitelist_entry(
        entry_id, req.agent_role, req.tool_name, req.allowed,
    )
    return {"entry_id": entry_id, "status": "updated"}

@app.delete("/tool-whitelist/{entry_id}")
async def delete_tool_whitelist(entry_id: str):
    _db_().delete_tool_whitelist_entry(entry_id)
    return {"entry_id": entry_id, "status": "deleted"}

# ── Category 6: Reliability / SRE ───────────────────────────────────────

@app.get("/reliability")
async def list_reliability_metrics(agent_role: str = "", hours: int = Query(default=24), limit: int = 50):
    db = _db_()
    conds = [f"ts >= now() - INTERVAL %(h)s HOUR"]
    params: dict = {"h": hours, "limit": limit}
    if agent_role:
        conds.append("agent_role=%(r)s")
        params["r"] = agent_role
    where = "WHERE " + " AND ".join(conds)
    rows = db.fetch_all(
        f"SELECT agent_role, error_rate, p95_ms, availability, error_budget_consumed, ts "
        f"FROM otel.gov_reliability_metrics {where} ORDER BY ts DESC LIMIT %(limit)s",
        params,
    )
    return rows or []

@app.get("/reliability/summary")
async def reliability_summary(agent_role: str = "", hours: int = Query(default=24)):
    db = _db_()
    conds = [f"ts >= now() - INTERVAL %(h)s HOUR"]
    params: dict = {"h": hours}
    if agent_role:
        conds.append("agent_role=%(r)s")
        params["r"] = agent_role
    where = "WHERE " + " AND ".join(conds)
    rows = db.fetch_all(
        "SELECT agent_role, avg(error_rate) AS avg_error_rate, avg(p95_ms) AS avg_p95_ms, "
        "avg(availability) AS avg_availability, avg(error_budget_consumed) AS avg_budget_consumed "
        f"FROM otel.gov_reliability_metrics {where} GROUP BY agent_role ORDER BY avg_error_rate DESC",
        params,
    )
    return rows or []

@app.get("/slo")
async def list_slo_config():
    return _db_().get_slo_config()

@app.post("/slo")
async def upsert_slo(req: SloConfigRequest):
    _db_().save_slo_config(req.agent_role, req.target_availability, req.error_budget_pct,
                           req.slo_window_days, req.p95_target_ms, req.p99_target_ms)
    return {"ok": True}

# ── Category 6+12: Incidents ─────────────────────────────────────────────

@app.get("/incidents")
async def list_incidents(agent_role: str = "", status: str = "", hours: int = Query(default=168), limit: int = 50):
    db = _db_()
    conds = [f"detected_at >= now() - INTERVAL %(h)s HOUR"]
    params: dict = {"h": hours, "limit": limit}
    if agent_role:
        conds.append("agent_role=%(r)s")
        params["r"] = agent_role
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            conds.append("status=%(s)s")
            params["s"] = statuses[0]
        elif len(statuses) > 1:
            placeholders = ", ".join(f"%(s{i})s" for i in range(len(statuses)))
            conds.append(f"status IN ({placeholders})")
            for i, s in enumerate(statuses):
                params[f"s{i}"] = s
    where = "WHERE " + " AND ".join(conds)
    rows = db.fetch_all(
        f"SELECT incident_id, agent_role, incident_type, severity, status, "
        f"trigger_event, detail, recurrence_of, root_cause, "
        f"opened_at, detected_at, contained_at, resolved_at "
        f"FROM otel.gov_incidents {where} ORDER BY detected_at DESC LIMIT %(limit)s",
        params,
    )
    return rows or []

@app.get("/incidents/summary")
async def incidents_summary(agent_role: str = "", hours: int = Query(default=168)):
    import math
    def _safe_float(v: float, default: float) -> float:
        try:
            return v if math.isfinite(v) else default
        except Exception:
            return default

    db = _db_()
    window_days = max(1, hours // 24)
    try:
        mttd = _safe_float(compute_mttd(db, agent_role, window_days=window_days), -1.0)
    except Exception:
        mttd = -1.0
    try:
        mttc = _safe_float(compute_mttc(db, agent_role, window_days=window_days), -1.0)
    except Exception:
        mttc = -1.0
    try:
        mttr = _safe_float(compute_mttr(db, agent_role, window_days=window_days), -1.0)
    except Exception:
        mttr = -1.0
    try:
        recurrence = _safe_float(compute_recurrence_rate(db, agent_role, window_days=window_days), 0.0)
    except Exception:
        recurrence = 0.0
    try:
        conds = [f"detected_at >= now() - INTERVAL %(h)s HOUR"]
        params: dict = {"h": hours}
        if agent_role:
            conds.append("agent_role=%(r)s")
            params["r"] = agent_role
        where = "WHERE " + " AND ".join(conds)
        counts = db.fetch_one(
            f"SELECT countIf(status='open') AS open_count, countIf(status='resolved') AS resolved_count "
            f"FROM otel.gov_incidents {where}",
            params,
        ) or {}
    except Exception:
        counts = {}
    return {"mttd_minutes": mttd, "mttc_minutes": mttc, "mttr_minutes": mttr, "recurrence_rate": recurrence, **counts}

@app.put("/incidents/{incident_id}")
async def update_incident(incident_id: str, req: IncidentUpdateRequest):
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    contained = now if req.status == "contained" else ""
    resolved  = now if req.status == "resolved"  else ""
    _db_().update_incident(incident_id, req.status, req.root_cause, contained, resolved)
    return {"ok": True}

# ── Category 7: Behavior ─────────────────────────────────────────────────

@app.get("/behavior/events")
async def list_behavior_events(trace_id: str = "", hours: int = Query(default=24), limit: int = 100):
    db = _db_()
    conds = [f"ts >= now() - INTERVAL %(h)s HOUR"]
    params: dict = {"h": hours, "limit": limit}
    if trace_id:
        conds.append("trace_id=%(t)s")
        params["t"] = trace_id
    where = "WHERE " + " AND ".join(conds)
    return db.fetch_all(
        f"SELECT event_id, trace_id, agent_role, event_type, detail, score, ts "
        f"FROM otel.gov_behavior_events {where} ORDER BY ts DESC LIMIT %(limit)s",
        params,
    ) or []

@app.get("/behavior/summary")
async def behavior_summary(hours: int = 24):
    db = _db_()
    try:
        rows = db.fetch_all(
            "SELECT event_type, count() AS cnt FROM otel.gov_behavior_events "
            "WHERE ts >= now() - INTERVAL %(h)s HOUR GROUP BY event_type ORDER BY cnt DESC",
            {"h": hours},
        )
        type_map = {r["event_type"]: int(r.get("cnt", 0)) for r in (rows or [])}
    except Exception:
        rows, type_map = [], {}
    try:
        scores = db.fetch_all(
            "SELECT metric, avg(value) AS avg_val FROM otel.gov_metric_snapshots "
            "WHERE metric IN ('consistency_score','persona_adherence_score','scope_violation_rate','behavioral_drift_score') "
            "AND ts >= now() - INTERVAL %(h)s HOUR GROUP BY metric",
            {"h": hours},
        )
    except Exception:
        scores = []
    return {
        "scope_violations":  type_map.get("scope_violation",  0),
        "persona_drifts":    type_map.get("persona_drift",    0),
        "total_events":      sum(type_map.values()),
        "events_by_type":    rows,
        "metric_averages":   scores,
    }

@app.get("/persona")
async def list_persona_config(agent_role: str = ""):
    return _db_().get_persona_config(agent_role=agent_role)

@app.post("/persona")
async def upsert_persona(req: PersonaConfigRequest):
    _db_().save_persona_config(req.agent_role, req.authorized_topics, req.persona_description)
    return {"ok": True}

# ── Category 11: Lifecycle ───────────────────────────────────────────────

@app.get("/lifecycle/changes")
async def list_change_log(agent_role: str = "", limit: int = 50):
    return _db_().get_change_log(agent_role=agent_role, limit=limit)

@app.get("/lifecycle/versions")
async def list_version_pins():
    return _db_().get_version_pins()

@app.post("/lifecycle/versions")
async def upsert_version_pin(req: VersionPinRequest):
    _db_().save_version_pin(req.agent_role, req.model_name, req.pinned_version, req.is_pinned, req.deployment_mode)
    return {"ok": True}

# ── Category 8+13: Regulatory ────────────────────────────────────────────

@app.get("/regulatory/scorecard")
async def get_scorecard(framework: str = ""):
    db = _db_()
    if framework:
        return compute_compliance_scorecard(db, framework)
    results = {}
    for fw in ["GDPR", "SOC2", "HIPAA", "EU_AI_ACT", "ISO42001"]:
        try:
            results[fw] = compute_compliance_scorecard(db, fw)
        except Exception as exc:
            results[fw] = {"error": str(exc)}
    return results

@app.get("/regulatory/scorecards/history")
async def scorecard_history(framework: str = ""):
    return _db_().get_compliance_scorecards(framework=framework)

@app.get("/regulatory/scope")
async def list_regulatory_scope(agent_role: str = ""):
    return _db_().get_regulatory_scope(agent_role=agent_role)

@app.post("/regulatory/scope")
async def add_regulatory_scope(req: RegulatoryScope):
    sid = _db_().save_regulatory_scope(req.agent_role, req.framework, req.sector, req.classification, req.notes)
    return {"scope_id": sid}

@app.get("/regulatory/risk-register")
async def list_risk_register():
    return _db_().get_risk_register()

@app.post("/regulatory/risk-register")
async def create_risk_item(req: RiskItemRequest):
    rid = _db_().save_risk_item(req.title, req.category, req.likelihood, req.impact,
                                req.owner, req.mitigation, req.status, req.risk_id)
    return {"risk_id": rid}

@app.put("/regulatory/risk-register/{risk_id}")
async def update_risk_item(risk_id: str, req: RiskItemRequest):
    rid = _db_().save_risk_item(req.title, req.category, req.likelihood, req.impact,
                                req.owner, req.mitigation, req.status, risk_id)
    return {"risk_id": rid}

# ── Category 8: Model Registry ───────────────────────────────────────────

# ── Category 14: Configurable Thresholds ─────────────────────────────────────

class ThresholdRequest(BaseModel):
    value:        float
    display_name: str  = ""
    category:     str  = ""
    unit:         str  = ""
    description:  str  = ""
    updated_by:   str  = "user"

@app.get("/thresholds")
async def list_thresholds(category: str = ""):
    rows = _db_().get_threshold_config_all()
    if category:
        rows = [r for r in rows if r.get("category") == category]
    return rows

@app.get("/thresholds/{config_key:path}")
async def get_threshold(config_key: str):
    rows = _db_().get_threshold_config_all()
    for r in rows:
        if r.get("config_key") == config_key:
            return r
    raise HTTPException(status_code=404, detail=f"Threshold '{config_key}' not found")

@app.put("/thresholds/{config_key:path}")
async def upsert_threshold(config_key: str, req: ThresholdRequest):
    _db_().save_threshold(
        config_key=config_key, value=req.value,
        display_name=req.display_name, category=req.category,
        unit=req.unit, description=req.description,
        updated_by=req.updated_by,
    )
    return {"ok": True, "config_key": config_key, "value": req.value}

@app.delete("/thresholds/{config_key:path}")
async def reset_threshold(config_key: str):
    """Reset a threshold to its seeded default by deleting the override."""
    _db_().delete_threshold(config_key)
    # Re-seed the default
    defaults = {row[0]: row for row in _THRESHOLD_DEFAULTS}
    if config_key in defaults:
        row = defaults[config_key]
        _db_().save_threshold(
            config_key=row[0], value=float(row[3]),
            display_name=row[1], category=row[2],
            unit=row[4], description=row[5], updated_by="system",
        )
        return {"ok": True, "reset_to_default": float(row[3])}
    return {"ok": True, "note": "key removed, no default to restore"}

@app.post("/thresholds/reset-all")
async def reset_all_thresholds():
    """Wipe all threshold overrides and re-seed defaults."""
    db = _db_()
    for (key, display_name, category, value, unit, description) in _THRESHOLD_DEFAULTS:
        db.save_threshold(
            config_key=key, value=float(value),
            display_name=display_name, category=category,
            unit=unit, description=description, updated_by="system",
        )
    return {"ok": True, "seeded": len(_THRESHOLD_DEFAULTS)}

@app.get("/model-registry")
async def list_model_registry():
    return _db_().get_model_registry()

@app.post("/model-registry")
async def add_model_registry(req: ModelRegistryRequest):
    mid = _db_().save_model_registry_entry(req.model_name, req.model_version, req.provider,
                                           req.license_type, req.commercial_ok, req.dpa_signed,
                                           req.baa_signed, req.sectors_allowed, req.notes)
    return {"registry_id": mid}

# ── Governance summary (expanded) ────────────────────────────────────────

@app.get("/governance/full-summary")
async def full_governance_summary(hours: int = 24):
    db = _db_()
    base = db.fetch_one(
        "SELECT count(DISTINCT trace_id) AS traces_scanned, "
        "countIf(metric='pii_leak_rate' AND value>0) AS pii_output_hits, "
        "avg(if(metric='prompt_snapshot_coverage', value, NULL)) AS snapshot_coverage, "
        "avg(if(metric='pii_leak_rate', value, NULL)) AS avg_pii_leak_rate "
        "FROM otel.gov_metric_snapshots WHERE ts >= now() - INTERVAL %(h)s HOUR",
        {"h": hours}
    ) or {}
    safety = db.fetch_one("SELECT count() AS total FROM otel.gov_safety_events WHERE ts >= now() - INTERVAL %(h)s HOUR AND detected=1", {"h": hours}) or {}
    identity = db.fetch_one("SELECT count() AS total FROM otel.gov_identity_events WHERE ts >= now() - INTERVAL %(h)s HOUR", {"h": hours}) or {}
    incidents = db.fetch_one(
        "SELECT countIf(status='open' AND opened_at >= now() - INTERVAL %(h)s HOUR) AS open_windowed, "
        "countIf(status='open') AS open_all "
        "FROM otel.gov_incidents",
        {"h": hours}
    ) or {}
    behavior = db.fetch_one("SELECT count() AS violations FROM otel.gov_behavior_events WHERE ts >= now() - INTERVAL %(h)s HOUR AND event_type='scope_violation'", {"h": hours}) or {}
    return {**base, "safety_events": safety.get("total", 0),
            "identity_events": identity.get("total", 0),
            "open_incidents": incidents.get("open_windowed", 0),
            "open_incidents_all": incidents.get("open_all", 0),
            "scope_violations": behavior.get("violations", 0)}


# ── Enforcement (Phase 1) ──────────────────────────────────────────────────────

class QuarantineRequest(BaseModel):
    reason: str = "manual_quarantine"

@app.get("/enforcement/trust-scores")
async def get_trust_scores():
    return _db_().get_latest_trust_scores()

@app.get("/enforcement/trust-scores/{agent_role}/history")
async def get_trust_score_history(agent_role: str, limit: int = 50):
    return _db_().get_trust_score_history(agent_role, limit)

@app.get("/enforcement/rogue-assessments")
async def get_rogue_assessments():
    return _db_().get_latest_rogue_assessments()

@app.get("/enforcement/rogue-assessments/{agent_role}/history")
async def get_rogue_history(agent_role: str, limit: int = 50):
    return _db_().get_rogue_assessment_history(agent_role, limit)

@app.get("/enforcement/burn-rates")
async def get_all_burn_rates():
    db = _db_()
    roles_row = db.fetch_all(
        "SELECT DISTINCT agent_role FROM otel.gov_reliability_metrics "
        "WHERE ts >= now() - INTERVAL 24 HOUR AND agent_role != ''",
        {}
    )
    roles = [r["agent_role"] for r in roles_row] if roles_row else []
    results = []
    for role in roles:
        try:
            br = compute_burn_rates(db, role)
            results.append({
                "agent_role":       br.agent_role,
                "burn_1h":          br.burn_1h,
                "burn_6h":          br.burn_6h,
                "burn_24h":         br.burn_24h,
                "alert_level":      br.alert_level,
                "error_budget_pct": br.error_budget_pct,
            })
        except Exception:
            pass
    return results

@app.get("/enforcement/burn-rates/{agent_role}")
async def get_burn_rates(agent_role: str):
    br = compute_burn_rates(_db_(), agent_role)
    return {
        "agent_role":       br.agent_role,
        "burn_1h":          br.burn_1h,
        "burn_6h":          br.burn_6h,
        "burn_24h":         br.burn_24h,
        "alert_level":      br.alert_level,
        "error_budget_pct": br.error_budget_pct,
    }

@app.get("/enforcement/circuit-breakers")
async def get_circuit_breakers():
    return _db_().get_all_circuit_breakers()

@app.post("/enforcement/circuit-breakers/{agent_role}/reset")
async def reset_circuit_breaker(agent_role: str):
    _db_().reset_circuit_breaker(agent_role)
    log.info("circuit_breaker_manual_reset", agent_role=agent_role)
    return {"ok": True, "agent_role": agent_role, "state": "closed"}

@app.post("/enforcement/circuit-breakers/{agent_role}/quarantine")
async def manual_quarantine(agent_role: str, req: QuarantineRequest):
    db = _db_()
    cb = db.get_circuit_breaker(agent_role)
    failure_threshold = int(cb.get("failure_threshold", 5) or 5)
    db.upsert_circuit_breaker(
        agent_role, "open",
        failure_threshold, failure_threshold, req.reason,
    )
    log.warning("manual_quarantine", agent_role=agent_role, reason=req.reason)
    return {"ok": True, "agent_role": agent_role, "state": "open", "reason": req.reason}

@app.post("/enforcement/circuit-breakers/{agent_role}/half-open")
async def manual_half_open(agent_role: str):
    db = _db_()
    cb = db.get_circuit_breaker(agent_role)
    failure_count     = int(cb.get("failure_count", 0) or 0)
    failure_threshold = int(cb.get("failure_threshold", 5) or 5)
    quarantine_reason = str(cb.get("quarantine_reason", "") or "")
    db.upsert_circuit_breaker(
        agent_role, "half_open",
        failure_count, failure_threshold, quarantine_reason,
    )
    log.info("circuit_breaker_manual_half_open", agent_role=agent_role)
    return {"ok": True, "agent_role": agent_role, "state": "half_open"}

@app.post("/enforcement/run-cycle/{agent_role}")
async def run_enforcement(agent_role: str):
    result = run_enforcement_cycle(_db_(), agent_role)
    return result

@app.get("/enforcement/phase2/status")
async def get_phase2_status():
    """Return whether Phase 2 pre-execution enforcement is enabled."""
    db = _db_()
    try:
        row = db.fetch_one(
            "SELECT value FROM otel.gov_threshold_config FINAL "
            "WHERE config_key = 'enforcement.phase2_enabled'",
            {},
        )
        enabled = bool(row and float(row.get("value", 0) or 0) >= 1.0)
    except Exception:
        enabled = False
    return {"phase2_enabled": enabled}

@app.post("/enforcement/phase2/enable")
async def enable_phase2():
    """Enable Phase 2 pre-execution enforcement."""
    _db_().save_threshold(
        config_key="enforcement.phase2_enabled", value=1.0,
        display_name="Pre-Execution Enforcement Enabled", category="enforcement",
        unit="bool", description="Pre-execution gate enforcement active",
        updated_by="ui",
    )
    log.info("phase2_enforcement_enabled")
    return {"phase2_enabled": True}

@app.post("/enforcement/phase2/disable")
async def disable_phase2():
    """Disable Phase 2 pre-execution enforcement (observe-only mode)."""
    _db_().save_threshold(
        config_key="enforcement.phase2_enabled", value=0.0,
        display_name="Pre-Execution Enforcement Enabled", category="enforcement",
        unit="bool", description="Pre-execution gate enforcement active",
        updated_by="ui",
    )
    log.info("phase2_enforcement_disabled")
    return {"phase2_enabled": False}


# ── Quality Gates ─────────────────────────────────────────────────────────────

class QualityGateConfigRequest(BaseModel):
    agent_role:  str   = "*"
    metric:      str
    threshold:   float
    action:      str   = "flag"   # flag | hold | block
    enabled:     int   = 1
    description: str   = ""


@app.get("/quality-gates/status")
async def get_quality_gates_status():
    db = _db_()
    try:
        row = db.fetch_one(
            "SELECT value FROM otel.gov_threshold_config FINAL "
            "WHERE config_key = 'enforcement.quality_gates_enabled'", {},
        )
        enabled = bool(row and float(row.get("value", 0) or 0) >= 1.0)
    except Exception:
        enabled = False
    return {"quality_gates_enabled": enabled}


@app.post("/quality-gates/enable")
async def enable_quality_gates():
    _db_().save_threshold(
        config_key="enforcement.quality_gates_enabled", value=1.0,
        display_name="Quality Gates Enabled", category="enforcement",
        unit="bool", description="Content quality gate checks active",
        updated_by="ui",
    )
    log.info("quality_gates_enabled")
    return {"quality_gates_enabled": True}


@app.post("/quality-gates/disable")
async def disable_quality_gates():
    _db_().save_threshold(
        config_key="enforcement.quality_gates_enabled", value=0.0,
        display_name="Quality Gates Enabled", category="enforcement",
        unit="bool", description="Content quality gate checks active",
        updated_by="ui",
    )
    log.info("quality_gates_disabled")
    return {"quality_gates_enabled": False}


@app.get("/quality-gates")
async def list_quality_gates():
    return {"gates": _db_().get_quality_gate_configs()}


@app.post("/quality-gates")
async def create_quality_gate(req: QualityGateConfigRequest):
    db = _db_()
    gate_id = db.save_quality_gate_config(
        agent_role=req.agent_role,
        metric=req.metric,
        threshold=req.threshold,
        action=req.action,
        enabled=req.enabled,
        description=req.description,
    )
    log.info("quality_gate_created", gate_id=gate_id, metric=req.metric)
    return {"gate_id": gate_id, "ok": True}


@app.put("/quality-gates/{gate_id}")
async def update_quality_gate(gate_id: str, req: QualityGateConfigRequest):
    db = _db_()
    db.save_quality_gate_config(
        agent_role=req.agent_role,
        metric=req.metric,
        threshold=req.threshold,
        action=req.action,
        enabled=req.enabled,
        description=req.description,
        gate_id=gate_id,
    )
    return {"gate_id": gate_id, "ok": True}


@app.delete("/quality-gates/{gate_id}")
async def delete_quality_gate(gate_id: str):
    _db_().delete_quality_gate_config(gate_id)
    return {"gate_id": gate_id, "ok": True}


@app.get("/quality-gates/decisions")
async def get_quality_gate_decisions(
    agent_role: str = Query(""),
    status:     str = Query(""),
    hours:      int = Query(24),
    limit:      int = Query(200),
):
    rows = _db_().get_quality_gate_decisions(
        agent_role=agent_role, status=status, hours=hours, limit=limit,
    )
    return {"decisions": rows}
