"""Governance Enforcement Engine — Phase 1.

Implements:
  compute_trust_score(db, agent_role)     → TrustScore
  assess_rogue_risk(db, agent_role)       → RogueAssessment
  compute_burn_rates(db, agent_role)      → BurnRates
  evaluate_circuit_breaker(db, agent_role, thresholds) → CircuitBreakerState
  run_enforcement_cycle(db, agent_role)   → EnforcementResult
"""

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone

import structlog

log = structlog.get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Result dataclasses
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class TrustScore:
    agent_role:       str
    trust_score:      float   # 0–1000
    identity_score:   float
    behavior_score:   float
    compliance_score: float
    network_score:    float
    trust_tier:       str     # critical / low / medium / high / verified


@dataclass
class RogueAssessment:
    agent_role:             str
    composite_score:        float   # 0.0–1.0; higher = more suspicious
    frequency_score:        float
    entropy_score:          float
    capability_score:       float
    risk_level:             str     # low / medium / high / critical
    quarantine_recommended: bool
    detail:                 str


@dataclass
class BurnRates:
    agent_role:          str
    burn_1h:             float   # error_rate / budget_pct in last 1h
    burn_6h:             float
    burn_24h:            float
    sustainable_rate:    float   # = 1.0 (budget sustained over window)
    alert_level:         str     # ok / warning (>2x) / critical (>10x)
    error_budget_pct:    float


@dataclass
class CircuitBreakerState:
    agent_role:         str
    state:              str     # closed / open / half_open
    failure_count:      int
    failure_threshold:  int
    quarantine_reason:  str
    transitioned:       bool    # True if state changed this cycle


# ──────────────────────────────────────────────────────────────────────────────
# Trust score
# ──────────────────────────────────────────────────────────────────────────────

_TRUST_TIERS = [
    (800, "verified"),
    (600, "high"),
    (400, "medium"),
    (200, "low"),
    (0,   "critical"),
]


def _tier(score: float) -> str:
    for threshold, name in _TRUST_TIERS:
        if score >= threshold:
            return name
    return "critical"


def compute_trust_score(db, agent_role: str, window_hours: int = 24) -> TrustScore:
    """Compute a 0–1000 composite trust score for agent_role.

    Components (weights):
      identity_score   25% — supply chain violations, credential leaks
      behavior_score   20% — safety events, anomaly severity
      compliance_score 25% — policy blocks, incident count
      network_score    15% — unregistered agent events, tool violations
      (reliability)    15% — SLA breach, error rate (folded into compliance)
    """
    h = int(window_hours)

    # ── identity component (25%) ──────────────────────────────────────────────
    identity_score = 1000.0
    try:
        id_row = db.fetch_one(
            "SELECT count() AS total, countIf(passed = 0) AS failed "
            "FROM otel.gov_identity_events "
            "WHERE agent_role = %(role)s AND ts >= now() - INTERVAL %(h)s HOUR",
            {"role": agent_role, "h": h},
        )
        if id_row and int(id_row.get("total", 0) or 0) > 0:
            fail_rate = int(id_row.get("failed", 0) or 0) / int(id_row["total"])
            identity_score = max(0.0, 1000.0 * (1.0 - fail_rate * 2.0))
    except Exception as exc:
        log.warning("trust_identity_query_failed", agent_role=agent_role, error=str(exc))

    # ── behavior component (20%) ─────────────────────────────────────────────
    behavior_score = 1000.0
    try:
        sev_weights = {"p1": 200, "critical": 200, "p2": 80, "high": 80,
                       "medium": 30, "low": 10}
        safety_rows = db.fetch_all(
            "SELECT event_type, count() AS cnt "
            "FROM otel.gov_safety_events "
            "WHERE agent_role = %(role)s AND ts >= now() - INTERVAL %(h)s HOUR "
            "GROUP BY event_type",
            {"role": agent_role, "h": h},
        )
        penalty = 0
        for r in safety_rows:
            # safety events default weight = 40
            penalty += int(r.get("cnt", 0) or 0) * 40
        # anomaly events
        anomaly_rows = db.fetch_all(
            "SELECT severity, count() AS cnt "
            "FROM otel.gov_anomaly_events "
            "WHERE agent_role = %(role)s AND ts >= now() - INTERVAL %(h)s HOUR "
            "GROUP BY severity",
            {"role": agent_role, "h": h},
        )
        for r in anomaly_rows:
            sev = str(r.get("severity", "") or "")
            penalty += int(r.get("cnt", 0) or 0) * sev_weights.get(sev.lower(), 20)
        behavior_score = max(0.0, 1000.0 - float(penalty))
    except Exception as exc:
        log.warning("trust_behavior_query_failed", agent_role=agent_role, error=str(exc))

    # ── compliance component (25%) ────────────────────────────────────────────
    compliance_score = 1000.0
    try:
        pol_row = db.fetch_one(
            "SELECT countIf(decision='block') AS blocks, "
            "countIf(decision='warn') AS warns "
            "FROM otel.gov_policy_decisions "
            "WHERE trace_id IN ("
            "  SELECT DISTINCT trace_id FROM otel.gov_audit_log "
            "  WHERE event_type='governance_eval' "
            "  AND ts >= now() - INTERVAL %(h)s HOUR"
            ")",
            {"h": h},
        )
        if pol_row:
            blocks = int(pol_row.get("blocks", 0) or 0)
            warns  = int(pol_row.get("warns",  0) or 0)
            compliance_score = max(0.0, 1000.0 - blocks * 100.0 - warns * 20.0)

        # open incidents further reduce compliance
        inc_row = db.fetch_one(
            "SELECT count() AS cnt FROM otel.gov_incidents "
            "WHERE agent_role = %(role)s AND status = 'open' "
            "  AND opened_at >= now() - INTERVAL %(h)s HOUR",
            {"role": agent_role, "h": h},
        )
        if inc_row:
            compliance_score = max(0.0, compliance_score - int(inc_row.get("cnt", 0) or 0) * 80.0)
    except Exception as exc:
        log.warning("trust_compliance_query_failed", agent_role=agent_role, error=str(exc))

    # ── network component (15%) — unregistered / tool violations ─────────────
    network_score = 1000.0
    try:
        net_row = db.fetch_one(
            "SELECT count() AS cnt FROM otel.gov_identity_events "
            "WHERE agent_role = %(role)s "
            "  AND event_type IN ('supply_chain_violation', 'tool_violation', 'unregistered_agent') "
            "  AND ts >= now() - INTERVAL %(h)s HOUR",
            {"role": agent_role, "h": h},
        )
        if net_row:
            network_score = max(0.0, 1000.0 - int(net_row.get("cnt", 0) or 0) * 60.0)
    except Exception as exc:
        log.warning("trust_network_query_failed", agent_role=agent_role, error=str(exc))

    # ── composite (weighted average) ──────────────────────────────────────────
    trust_score = (
        identity_score   * 0.25 +
        behavior_score   * 0.20 +
        compliance_score * 0.40 +   # reliability folded into compliance weight
        network_score    * 0.15
    )
    trust_score = round(max(0.0, min(1000.0, trust_score)), 1)
    tier        = _tier(trust_score)

    db.save_trust_score(
        agent_role, trust_score,
        round(identity_score, 1), round(behavior_score, 1),
        round(compliance_score, 1), round(network_score, 1),
        tier,
    )
    log.info("trust_score_computed", agent_role=agent_role,
             trust_score=trust_score, tier=tier)
    return TrustScore(
        agent_role=agent_role,
        trust_score=trust_score,
        identity_score=round(identity_score, 1),
        behavior_score=round(behavior_score, 1),
        compliance_score=round(compliance_score, 1),
        network_score=round(network_score, 1),
        trust_tier=tier,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Rogue agent assessment
# ──────────────────────────────────────────────────────────────────────────────

def assess_rogue_risk(db, agent_role: str, window_minutes: int = 60) -> RogueAssessment:
    """Detect rogue behavior from three signals:
      1. Tool-call frequency spike (Z-score vs baseline)
      2. Tool-call entropy (too low = loop, too high = chaos)
      3. Capability violations (tools called outside whitelist)
    """
    h = max(1, window_minutes // 60)

    # ── 1. Frequency score ────────────────────────────────────────────────────
    frequency_score = 0.0
    try:
        # Current period
        cur = db.fetch_one(
            "SELECT count() AS cnt FROM otel.otel_traces "
            "WHERE SpanName = 'agent.tool_call' "
            "  AND SpanAttributes['agent.role'] = %(role)s "
            "  AND Timestamp >= now() - INTERVAL %(h)s HOUR",
            {"role": agent_role, "h": h},
        )
        cur_count = int((cur or {}).get("cnt", 0) or 0)

        # Baseline: 7-day rolling mean/stddev
        base = db.fetch_one(
            "SELECT avg(daily) AS mean, stddevPop(daily) AS stddev FROM ("
            "  SELECT toDate(Timestamp) AS day, count() AS daily "
            "  FROM otel.otel_traces "
            "  WHERE SpanName = 'agent.tool_call' "
            "    AND SpanAttributes['agent.role'] = %(role)s "
            "    AND Timestamp >= now() - INTERVAL 7 DAY "
            "  GROUP BY day"
            ")",
            {"role": agent_role},
        )
        if base:
            mean   = float(base.get("mean",   0) or 0)
            stddev = float(base.get("stddev", 0) or 0)
            if stddev > 0 and mean > 0:
                z = (cur_count - mean) / stddev
                # Z > 2.5 is suspicious; cap at 1.0
                frequency_score = min(1.0, max(0.0, (z - 2.5) / 5.0)) if z > 2.5 else 0.0
    except Exception as exc:
        log.warning("rogue_frequency_failed", agent_role=agent_role, error=str(exc))

    # ── 2. Entropy score ─────────────────────────────────────────────────────
    entropy_score = 0.0
    try:
        tool_rows = db.fetch_all(
            "SELECT SpanAttributes['tool.name'] AS tool_name, count() AS cnt "
            "FROM otel.otel_traces "
            "WHERE SpanName = 'agent.tool_call' "
            "  AND SpanAttributes['agent.role'] = %(role)s "
            "  AND Timestamp >= now() - INTERVAL %(h)s HOUR "
            "GROUP BY tool_name",
            {"role": agent_role, "h": h},
        )
        if tool_rows:
            total = sum(int(r.get("cnt", 0) or 0) for r in tool_rows)
            if total > 0:
                probs = [int(r.get("cnt", 0) or 0) / total for r in tool_rows]
                # Shannon entropy
                entropy = -sum(p * math.log2(p) for p in probs if p > 0)
                max_entropy = math.log2(len(tool_rows)) if len(tool_rows) > 1 else 1.0
                # Too low (<0.3 normalised = repetitive loop) or too high (>0.95 = chaos)
                norm = entropy / max_entropy if max_entropy > 0 else 0.0
                if norm < 0.3:
                    entropy_score = (0.3 - norm) / 0.3   # 0→1 as norm→0
                elif norm > 0.95:
                    entropy_score = (norm - 0.95) / 0.05  # 0→1 as norm→1
    except Exception as exc:
        log.warning("rogue_entropy_failed", agent_role=agent_role, error=str(exc))

    # ── 3. Capability violation score ────────────────────────────────────────
    capability_score = 0.0
    try:
        viol_row = db.fetch_one(
            "SELECT count() AS cnt FROM otel.gov_identity_events "
            "WHERE agent_role = %(role)s AND event_type = 'tool_violation' "
            "  AND ts >= now() - INTERVAL %(h)s HOUR",
            {"role": agent_role, "h": h},
        )
        viol_count = int((viol_row or {}).get("cnt", 0) or 0)
        capability_score = min(1.0, viol_count / 5.0)
    except Exception as exc:
        log.warning("rogue_capability_failed", agent_role=agent_role, error=str(exc))

    # ── composite ─────────────────────────────────────────────────────────────
    composite = (
        frequency_score  * 0.40 +
        entropy_score    * 0.35 +
        capability_score * 0.25
    )
    composite = round(min(1.0, max(0.0, composite)), 4)

    if composite >= 0.75:
        risk_level = "critical"
    elif composite >= 0.50:
        risk_level = "high"
    elif composite >= 0.25:
        risk_level = "medium"
    else:
        risk_level = "low"

    quarantine = composite >= 0.75

    detail = json.dumps({
        "frequency_score":  round(frequency_score, 4),
        "entropy_score":    round(entropy_score, 4),
        "capability_score": round(capability_score, 4),
        "window_hours":     h,
    })

    db.save_rogue_assessment(
        agent_role, composite, frequency_score, entropy_score,
        capability_score, risk_level, quarantine, detail,
    )

    log.info("rogue_assessment_complete", agent_role=agent_role,
             composite=composite, risk_level=risk_level,
             quarantine_recommended=quarantine)

    return RogueAssessment(
        agent_role=agent_role,
        composite_score=composite,
        frequency_score=round(frequency_score, 4),
        entropy_score=round(entropy_score, 4),
        capability_score=round(capability_score, 4),
        risk_level=risk_level,
        quarantine_recommended=quarantine,
        detail=detail,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Burn rates
# ──────────────────────────────────────────────────────────────────────────────

def compute_burn_rates(db, agent_role: str) -> BurnRates:
    """Compute error budget burn rate across 1h / 6h / 24h windows."""

    # Fetch SLO config
    error_budget_pct = 0.001
    try:
        slo = db.fetch_one(
            "SELECT error_budget_pct FROM otel.gov_slo_config FINAL "
            "WHERE agent_role = %(role)s",
            {"role": agent_role},
        )
        if not slo:
            slo = db.fetch_one(
                "SELECT error_budget_pct FROM otel.gov_slo_config FINAL "
                "WHERE agent_role = '__default__'"
            )
        if slo:
            error_budget_pct = float(slo.get("error_budget_pct", 0.001) or 0.001)
    except Exception as exc:
        log.warning("burn_rate_slo_query_failed", agent_role=agent_role, error=str(exc))

    def _rate_for_window(hours: int) -> float:
        try:
            row = db.fetch_one(
                "SELECT count() AS total, "
                "countIf(StatusCode = 'STATUS_CODE_ERROR') AS errors "
                "FROM otel.otel_traces "
                "WHERE SpanName = 'agent.task' "
                "  AND SpanAttributes['agent.role'] = %(role)s "
                "  AND Timestamp >= now() - INTERVAL %(h)s HOUR",
                {"role": agent_role, "h": hours},
            )
            if row and int(row.get("total", 0) or 0) > 0:
                err_rate = int(row.get("errors", 0) or 0) / int(row["total"])
                if error_budget_pct > 0:
                    return round(err_rate / error_budget_pct, 3)
        except Exception as exc:
            log.warning("burn_rate_window_failed", agent_role=agent_role,
                        hours=hours, error=str(exc))
        return 0.0

    burn_1h  = _rate_for_window(1)
    burn_6h  = _rate_for_window(6)
    burn_24h = _rate_for_window(24)

    if burn_1h >= 10.0 or burn_6h >= 10.0:
        alert_level = "critical"
    elif burn_1h >= 2.0 or burn_6h >= 2.0 or burn_24h >= 2.0:
        alert_level = "warning"
    else:
        alert_level = "ok"

    return BurnRates(
        agent_role=agent_role,
        burn_1h=burn_1h,
        burn_6h=burn_6h,
        burn_24h=burn_24h,
        sustainable_rate=1.0,
        alert_level=alert_level,
        error_budget_pct=error_budget_pct,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Circuit breaker
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_FAILURE_THRESHOLD = 5

def evaluate_circuit_breaker(
    db,
    agent_role: str,
    thresholds: dict | None = None,
) -> CircuitBreakerState:
    """Evaluate circuit breaker state for agent_role.

    Opens breaker when recent SLA breach or rogue critical risk detected.
    Transitions OPEN → HALF_OPEN after recovery_minutes.
    """
    t = thresholds or {}
    failure_threshold  = int(t.get("cb.failure_threshold",  _DEFAULT_FAILURE_THRESHOLD))
    recovery_minutes   = int(t.get("cb.recovery_minutes",   30))

    current = db.get_circuit_breaker(agent_role)
    record_exists = bool(current)
    state         = str(current.get("state", "closed") or "closed")
    failure_count = int(current.get("failure_count", 0) or 0)
    quarantine_reason = str(current.get("quarantine_reason", "") or "")
    transitioned  = False

    # Count recent incidents (SLA breach / rogue / critical anomaly)
    try:
        inc_row = db.fetch_one(
            "SELECT count() AS cnt FROM otel.gov_incidents "
            "WHERE agent_role = %(role)s "
            "  AND status = 'open' "
            "  AND severity IN ('p1', 'p2', 'critical', 'high') "
            "  AND opened_at >= now() - INTERVAL 1 HOUR",
            {"role": agent_role},
        )
        recent_incidents = int((inc_row or {}).get("cnt", 0) or 0)
    except Exception:
        recent_incidents = 0

    # Check latest rogue assessment
    rogue_critical = False
    try:
        rogue_row = db.fetch_one(
            "SELECT risk_level, quarantine_recommended FROM otel.gov_rogue_assessments "
            "WHERE agent_role = %(role)s "
            "ORDER BY assessed_at DESC LIMIT 1",
            {"role": agent_role},
        )
        if rogue_row:
            rogue_critical = (
                str(rogue_row.get("risk_level", "") or "") in ("critical", "high")
                and int(rogue_row.get("quarantine_recommended", 0) or 0) == 1
            )
    except Exception:
        pass

    if state == "closed":
        # Use recent_incidents as a snapshot (not cumulative add) so incidents
        # are not double-counted on every watcher tick while they stay open.
        # Quality gate failures are tracked separately via record_quality_gate_cb_failure
        # and must not be overwritten here — take the max of the two paths.
        new_failures = max(failure_count, recent_incidents)
        if rogue_critical:
            new_failures = failure_threshold  # immediately open on rogue critical
        if new_failures >= failure_threshold:
            reason = "rogue_detected" if rogue_critical else f"{recent_incidents}_incidents_last_1h"
            db.upsert_circuit_breaker(agent_role, "open", new_failures,
                                      failure_threshold, reason)
            state = "open"
            quarantine_reason = reason
            transitioned = True
            log.warning("circuit_breaker_opened", agent_role=agent_role, reason=reason)
        elif new_failures != failure_count or not record_exists:
            db.upsert_circuit_breaker(agent_role, "closed", new_failures,
                                      failure_threshold, "")

    elif state == "open":
        # Check if recovery window has elapsed → move to half_open
        opened_at_raw = current.get("opened_at")
        if opened_at_raw:
            try:
                if isinstance(opened_at_raw, str):
                    from datetime import datetime
                    opened_at = datetime.fromisoformat(opened_at_raw.replace("Z", "+00:00"))
                else:
                    opened_at = opened_at_raw
                elapsed_min = (datetime.now(timezone.utc) - opened_at.replace(
                    tzinfo=timezone.utc if opened_at.tzinfo is None else opened_at.tzinfo
                )).total_seconds() / 60
                if elapsed_min >= recovery_minutes:
                    db.upsert_circuit_breaker(agent_role, "half_open",
                                              failure_count, failure_threshold,
                                              quarantine_reason)
                    state = "half_open"
                    transitioned = True
                    log.info("circuit_breaker_half_open", agent_role=agent_role)
            except Exception:
                pass

    elif state == "half_open":
        # Probe: if no new incidents, close; else reopen
        if recent_incidents == 0 and not rogue_critical:
            db.reset_circuit_breaker(agent_role)
            state = "closed"
            failure_count = 0
            quarantine_reason = ""
            transitioned = True
            log.info("circuit_breaker_closed", agent_role=agent_role)
        else:
            reason = "probe_failed"
            db.upsert_circuit_breaker(agent_role, "open",
                                      failure_threshold, failure_threshold, reason)
            state = "open"
            quarantine_reason = reason
            transitioned = True
            log.warning("circuit_breaker_reopened", agent_role=agent_role)

    return CircuitBreakerState(
        agent_role=agent_role,
        state=state,
        failure_count=failure_count,
        failure_threshold=failure_threshold,
        quarantine_reason=quarantine_reason,
        transitioned=transitioned,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Full enforcement cycle (called by runner)
# ──────────────────────────────────────────────────────────────────────────────

def run_enforcement_cycle(
    db,
    agent_role: str,
    thresholds: dict | None = None,
    window_hours: int = 24,
) -> dict:
    """Run all Phase 1 enforcement checks for agent_role. Returns summary dict."""
    trust    = compute_trust_score(db, agent_role, window_hours)
    rogue    = assess_rogue_risk(db, agent_role)
    burns    = compute_burn_rates(db, agent_role)
    cb       = evaluate_circuit_breaker(db, agent_role, thresholds)

    # Auto-open circuit breaker if rogue quarantine recommended
    if rogue.quarantine_recommended and cb.state == "closed":
        db.upsert_circuit_breaker(
            agent_role, "open",
            cb.failure_threshold, cb.failure_threshold,
            "auto_quarantine_rogue_detection",
        )
        cb = CircuitBreakerState(
            agent_role=agent_role,
            state="open",
            failure_count=cb.failure_threshold,
            failure_threshold=cb.failure_threshold,
            quarantine_reason="auto_quarantine_rogue_detection",
            transitioned=True,
        )
        log.warning("auto_quarantine_triggered", agent_role=agent_role,
                    rogue_score=rogue.composite_score)

    return {
        "agent_role":    agent_role,
        "trust_score":   trust.trust_score,
        "trust_tier":    trust.trust_tier,
        "rogue_risk":    rogue.risk_level,
        "rogue_score":   rogue.composite_score,
        "quarantine":    rogue.quarantine_recommended,
        "burn_1h":       burns.burn_1h,
        "burn_6h":       burns.burn_6h,
        "burn_24h":      burns.burn_24h,
        "burn_alert":    burns.alert_level,
        "cb_state":      cb.state,
        "cb_transitioned": cb.transitioned,
    }
