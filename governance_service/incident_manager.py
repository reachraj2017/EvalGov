"""Incident Response & Risk Mitigation — Category 12.

Automatically creates incidents from safety and reliability violations,
deduplicates against open incidents within a 30-day window, and computes
MTTD, MTTC, and recurrence rate metrics.

Key public API:
  auto_detect_incidents(db, trace_id, run_id, agent_role,
                        anomalies, safety_result, reliability_result)
                                                           → list[IncidentRecord]
  compute_mttd(db, agent_role, window_days=30)             → float (minutes)
  compute_mttc(db, agent_role, window_days=30)             → float (minutes)
  compute_recurrence_rate(db, agent_role, window_days=30)  → float
"""

import json
import uuid
from dataclasses import dataclass

import structlog

log = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class IncidentRecord:
    incident_id:   str
    agent_role:    str
    incident_type: str
    severity:      str
    trigger:       str
    is_recurrence: bool
    recurrence_of: str


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _find_open_incident(db, agent_role: str, incident_type: str, window_days: int = 30) -> str:
    """Return the incident_id of the most recent open incident for this
    (agent_role, incident_type) pair within the last 30 days, or '' if none."""
    try:
        row = db.fetch_one(
            f"SELECT incident_id FROM otel.gov_incidents "
            f"WHERE agent_role = %(role)s AND incident_type = %(type)s "
            f"  AND status = 'open' "
            f"  AND opened_at >= now() - INTERVAL {int(window_days)} DAY "
            f"ORDER BY opened_at DESC LIMIT 1",
            {"role": agent_role, "type": incident_type},
        )
        if row:
            return str(row.get("incident_id", "") or "")
    except Exception as exc:
        log.warning("incident_open_query_failed",
                    agent_role=agent_role, incident_type=incident_type,
                    error=str(exc))
    return ""


def _insert_incident(
    db,
    incident_id:   str,
    trace_id:      str,
    run_id:        str,
    agent_role:    str,
    incident_type: str,
    severity:      str,
    trigger:       str,
    recurrence_of: str,
) -> None:
    """Write a new row into otel.gov_incidents."""
    try:
        db.execute(
            "INSERT INTO otel.gov_incidents "
            "(incident_id, agent_role, incident_type, "
            "severity, trigger_event, recurrence_of, status, detail) VALUES",
            [(incident_id, agent_role, incident_type,
              severity, trigger, recurrence_of, "open",
              json.dumps({"trace_id": trace_id, "run_id": run_id}))],
        )
    except Exception as exc:
        log.warning("incident_insert_failed",
                    incident_id=incident_id, agent_role=agent_role,
                    error=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

def auto_detect_incidents(
    db,
    trace_id:           str,
    run_id:             str,
    agent_role:         str,
    anomalies:          list,
    safety_result,
    reliability_result,
    thresholds:    dict = None,
    policy_blocks: int  = 0,
) -> list[IncidentRecord]:
    """Automatically create incidents from safety and reliability violations.

    Deduplicates: if an open incident for the same agent_role+incident_type
    already exists within 30 days, the new record is marked as a recurrence
    and linked via recurrence_of rather than creating a wholly new incident.

    Args:
        db:                 GovernanceDB instance.
        trace_id:           OTel trace ID that triggered the evaluation.
        run_id:             Governance run ID.
        agent_role:         Role of the agent being evaluated.
        anomalies:          List of AnomalyResult objects from anomaly_detector.
        safety_result:      SafetyResult from safety_guard.
        reliability_result: ReliabilityResult from reliability_tracker.

    Returns:
        List of IncidentRecord instances for every incident created or noted.
    """
    t                  = thresholds or {}
    _budget_breach_mult = float(t.get("incident.error_budget_breach_multiplier", 2.0))
    _dedup_days         = int(t.get("incident.dedup_window_days",                30))
    # Collect (incident_type, severity, trigger) tuples to process
    candidates: list[tuple[str, str, str]] = []

    # ── P1: Safety incidents ──────────────────────────────────────────────
    if getattr(safety_result, "injection_detected", False):
        candidates.append(("safety", "p1", "prompt_injection_detected"))
    if getattr(safety_result, "jailbreak_detected", False):
        candidates.append(("safety", "p1", "jailbreak_detected"))

    # ── P2: Toxic / bias incidents ────────────────────────────────────────
    if int(getattr(safety_result, "toxic_output_count", 0) or 0) > 0:
        candidates.append(("safety_toxic", "p2", "toxic_content_detected"))
    if getattr(safety_result, "bias_flagged", False):
        candidates.append(("safety_bias", "p2", "bias_detected"))

    # ── P2: Policy block incidents ────────────────────────────────────────
    _policy_blocks = int(t.get("incident.policy_blocks", policy_blocks or 0))
    if _policy_blocks > 0:
        candidates.append(("policy", "p2", f"policy_blocked_{_policy_blocks}_times"))

    # ── P2: Reliability incidents ─────────────────────────────────────────
    if getattr(reliability_result, "sla_breach", False):
        candidates.append(("reliability", "p2", "sla_breach"))
    error_budget_consumed = float(
        getattr(reliability_result, "error_budget_consumed", 0.0) or 0.0
    )
    if error_budget_consumed > _budget_breach_mult:
        candidates.append(("reliability", "p2", f"error_budget_consumed_gt_{_budget_breach_mult}x"))

    # ── P2: Critical anomalies ────────────────────────────────────────────
    for anomaly in (anomalies or []):
        if str(getattr(anomaly, "severity", "") or "").lower() == "critical":
            metric = str(getattr(anomaly, "metric", "unknown") or "unknown")
            candidates.append(("behavior", "p2", f"critical_anomaly:{metric}"))

    records: list[IncidentRecord] = []

    for incident_type, severity, trigger in candidates:
        existing_id = _find_open_incident(db, agent_role, incident_type, window_days=_dedup_days)
        is_recurrence = existing_id != ""
        recurrence_of = existing_id if is_recurrence else ""
        incident_id   = str(uuid.uuid4())

        _insert_incident(
            db,
            incident_id=incident_id,
            trace_id=trace_id,
            run_id=run_id,
            agent_role=agent_role,
            incident_type=incident_type,
            severity=severity,
            trigger=trigger,
            recurrence_of=recurrence_of,
        )

        records.append(IncidentRecord(
            incident_id=incident_id,
            agent_role=agent_role,
            incident_type=incident_type,
            severity=severity,
            trigger=trigger,
            is_recurrence=is_recurrence,
            recurrence_of=recurrence_of,
        ))

        log.warning(
            "incident_created",
            incident_id=incident_id,
            agent_role=agent_role,
            incident_type=incident_type,
            severity=severity,
            trigger=trigger,
            is_recurrence=is_recurrence,
            recurrence_of=recurrence_of,
        )

    return records


# ──────────────────────────────────────────────────────────────────────────────
# MTTD / MTTC / recurrence rate
# ──────────────────────────────────────────────────────────────────────────────

def compute_mttd(db, agent_role: str, window_days: int = 30) -> float:
    """Mean Time To Detect: average minutes from trace first span to
    governance_eval audit log entry (watcher pickup delay).

    When agent_role is empty, computes across all roles.
    Returns -1.0 if no data is available.
    """
    try:
        role_clause = "AND ae.agent_role = %(role)s " if agent_role else ""
        row = db.fetch_one(
            "SELECT avg(toUnixTimestamp(al.ts) - toUnixTimestamp(tr.min_ts)) "
            "  / 60.0 AS mttd_minutes "
            "FROM otel.gov_audit_log al "
            "JOIN ( "
            "  SELECT TraceId AS trace_id, min(Timestamp) AS min_ts "
            "  FROM otel.otel_traces "
            "  WHERE Timestamp >= now() - INTERVAL %(days)s DAY "
            "  GROUP BY TraceId "
            ") tr ON al.trace_id = tr.trace_id "
            "LEFT JOIN otel.gov_anomaly_events ae ON al.trace_id = ae.trace_id "
            f"WHERE al.event_type = 'governance_eval' "
            f"  AND al.ts >= now() - INTERVAL %(days)s DAY "
            f"  {role_clause}",
            {"role": agent_role, "days": int(window_days)} if agent_role
            else {"days": int(window_days)},
        )
        if row and row.get("mttd_minutes") is not None:
            val = float(row["mttd_minutes"] or 0.0)
            if val >= 0:
                return round(val, 3)
    except Exception as exc:
        log.warning("compute_mttd_failed", agent_role=agent_role, error=str(exc))
    return -1.0


def compute_mttc(db, agent_role: str, window_days: int = 30) -> float:
    """Mean Time To Contain: average minutes from incident opened_at to
    contained_at for manually contained incidents.

    When agent_role is empty, computes across all roles.
    Returns -1.0 if no contained incidents exist yet.
    """
    try:
        role_clause = "AND agent_role = %(role)s " if agent_role else ""
        row = db.fetch_one(
            "SELECT avg(toUnixTimestamp(contained_at) - toUnixTimestamp(opened_at)) "
            "  / 60.0 AS mttc_minutes "
            "FROM otel.gov_incidents "
            f"WHERE contained_at != '1970-01-01 00:00:00' "
            f"  AND contained_at >= now() - INTERVAL %(days)s DAY "
            f"  {role_clause}",
            {"role": agent_role, "days": int(window_days)} if agent_role
            else {"days": int(window_days)},
        )
        if row and row.get("mttc_minutes") is not None:
            val = float(row["mttc_minutes"] or 0.0)
            if val >= 0:
                return round(val, 3)
    except Exception as exc:
        log.warning("compute_mttc_failed", agent_role=agent_role, error=str(exc))
    return -1.0


def compute_mttr(db, agent_role: str, window_days: int = 30) -> float:
    """Mean Time To Resolve: average minutes from incident opened_at to
    resolved_at for fully resolved incidents.

    When agent_role is empty, computes across all roles.
    Returns -1.0 if no resolved incidents exist yet.
    """
    try:
        role_clause = "AND agent_role = %(role)s " if agent_role else ""
        row = db.fetch_one(
            "SELECT avg(toUnixTimestamp(resolved_at) - toUnixTimestamp(opened_at)) "
            "  / 60.0 AS mttr_minutes "
            "FROM otel.gov_incidents "
            f"WHERE resolved_at != '1970-01-01 00:00:00' "
            f"  AND resolved_at >= now() - INTERVAL %(days)s DAY "
            f"  {role_clause}",
            {"role": agent_role, "days": int(window_days)} if agent_role
            else {"days": int(window_days)},
        )
        if row and row.get("mttr_minutes") is not None:
            val = float(row["mttr_minutes"] or 0.0)
            if val >= 0:
                return round(val, 3)
    except Exception as exc:
        log.warning("compute_mttr_failed", agent_role=agent_role, error=str(exc))
    return -1.0


def compute_recurrence_rate(db, agent_role: str, window_days: int = 30) -> float:
    """Fraction of incidents that are recurrences of a prior open incident.

    When agent_role is empty, computes across all roles.
    Returns 0.0 if no incidents found.
    """
    try:
        role_clause = "AND agent_role = %(role)s " if agent_role else ""
        row = db.fetch_one(
            "SELECT "
            "  countIf(recurrence_of != '') AS recurrence_count, "
            "  count() AS total "
            "FROM otel.gov_incidents "
            f"WHERE opened_at >= now() - INTERVAL %(days)s DAY "
            f"  {role_clause}",
            {"role": agent_role, "days": int(window_days)} if agent_role
            else {"days": int(window_days)},
        )
        if row:
            total       = int(row.get("total",            0) or 0)
            recurrences = int(row.get("recurrence_count", 0) or 0)
            if total > 0:
                return round(recurrences / total, 6)
    except Exception as exc:
        log.warning("compute_recurrence_rate_failed",
                    agent_role=agent_role, error=str(exc))
    return 0.0


def write_incident_metrics(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    window_days: int = 30,
) -> None:
    """Compute and persist MTTD, MTTC, and incident_recurrence_rate snapshots.

    Intended to be called once per governance run, after auto_detect_incidents.

    Args:
        db:          GovernanceDB instance.
        trace_id:    OTel trace ID (used as the snapshot key).
        run_id:      Governance run ID.
        agent_role:  Agent role to compute metrics for.
        window_days: Lookback window passed to compute_* functions.
    """
    mttd = compute_mttd(db, agent_role, window_days)
    mttc = compute_mttc(db, agent_role, window_days)
    mttr = compute_mttr(db, agent_role, window_days)
    recurrence_rate = compute_recurrence_rate(db, agent_role, window_days)

    for metric, value, detail in [
        ("mttd_minutes",             mttd,            ""),
        ("mttc_minutes",             mttc,            ""),
        ("mttr_minutes",             mttr,            ""),
        ("incident_recurrence_rate", recurrence_rate, ""),
    ]:
        try:
            db.save_gov_metric(trace_id, "", run_id, metric, value, detail)
        except Exception as exc:
            log.warning("incident_metric_save_failed",
                        metric=metric, error=str(exc))

    log.info(
        "incident_metrics_written",
        trace_id=trace_id,
        agent_role=agent_role,
        mttd_minutes=mttd,
        mttc_minutes=mttc,
        incident_recurrence_rate=recurrence_rate,
    )
