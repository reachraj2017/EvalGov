"""Compliance report generator.

Produces structured evidence packages:
  summary  — aggregate KPIs + compliance score for a date range
  lineage  — data flow for a specific trace (which agents, what PII, what decisions)
"""

import json
import uuid
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


@dataclass
class ComplianceSummaryReport:
    report_id:           str
    report_type:         str
    from_ts:             str
    to_ts:               str
    generated_at:        str
    generated_by:        str
    traces_scanned:      int
    pii_events:          int
    gate_blocks:         int
    gate_warnings:       int
    drift_events:        int
    anomaly_events:      int
    remediation_actions: int
    top_blocked_metrics: list
    top_pii_traces:      list
    compliance_score:    float   # 0–100, higher is better


def generate_summary(
    db,
    from_ts: datetime,
    to_ts: datetime,
    generated_by: str = "system",
) -> ComplianceSummaryReport:
    """Generate and persist a compliance summary report."""
    report_id = str(uuid.uuid4())

    metrics_row = db.fetch_one(
        "SELECT count(DISTINCT trace_id) AS traces_scanned, "
        "countIf(metric = 'pii_in_output' AND value > 0) AS pii_events "
        "FROM otel.gov_metric_snapshots "
        "WHERE ts >= %(f)s AND ts <= %(t)s",
        {"f": from_ts, "t": to_ts},
    ) or {}

    policy_row = db.fetch_one(
        "SELECT countIf(decision='block') AS gate_blocks, "
        "countIf(decision='warn') AS gate_warnings "
        "FROM otel.gov_policy_decisions "
        "WHERE ts >= %(f)s AND ts <= %(t)s",
        {"f": from_ts, "t": to_ts},
    ) or {}

    anomaly_row = db.fetch_one(
        "SELECT count() AS anomaly_events FROM otel.gov_anomaly_events "
        "WHERE ts >= %(f)s AND ts <= %(t)s",
        {"f": from_ts, "t": to_ts},
    ) or {}

    drift_row = db.fetch_one(
        "SELECT countIf(drift_detected=1) AS drift_events "
        "FROM otel.gov_prompt_drift FINAL "
        "WHERE last_seen >= %(f)s AND last_seen <= %(t)s",
        {"f": from_ts, "t": to_ts},
    ) or {}

    remed_row = db.fetch_one(
        "SELECT count() AS remed FROM otel.gov_remediation_log "
        "WHERE ts >= %(f)s AND ts <= %(t)s",
        {"f": from_ts, "t": to_ts},
    ) or {}

    top_blocked = db.fetch_all(
        "SELECT metric, count() AS block_count "
        "FROM otel.gov_policy_decisions "
        "WHERE decision='block' AND ts >= %(f)s AND ts <= %(t)s "
        "GROUP BY metric ORDER BY block_count DESC LIMIT 5",
        {"f": from_ts, "t": to_ts},
    )

    top_pii = db.fetch_all(
        "SELECT trace_id, count() AS pii_count "
        "FROM otel.gov_metric_snapshots "
        "WHERE metric='pii_in_output' AND value>0 "
        "AND ts >= %(f)s AND ts <= %(t)s "
        "GROUP BY trace_id ORDER BY pii_count DESC LIMIT 5",
        {"f": from_ts, "t": to_ts},
    )

    n          = int(metrics_row.get("traces_scanned", 0) or 0)
    pii        = int(metrics_row.get("pii_events",    0) or 0)
    blocks     = int(policy_row.get("gate_blocks",    0) or 0)
    warnings   = int(policy_row.get("gate_warnings",  0) or 0)
    anomalies  = int(anomaly_row.get("anomaly_events",0) or 0)
    drifts     = int(drift_row.get("drift_events",    0) or 0)
    remeds     = int(remed_row.get("remed",           0) or 0)

    score = 100.0
    if n > 0:
        score -= min(40.0, pii       / n * 200)
        score -= min(30.0, blocks    / n * 150)
        score -= min(20.0, anomalies * 5)
        score -= min(10.0, drifts    * 2)
    score = max(0.0, round(score, 1))

    report = ComplianceSummaryReport(
        report_id=report_id,
        report_type="summary",
        from_ts=from_ts.strftime("%Y-%m-%d %H:%M:%S"),
        to_ts=to_ts.strftime("%Y-%m-%d %H:%M:%S"),
        generated_at=datetime.utcnow().isoformat(),
        generated_by=generated_by,
        traces_scanned=n,
        pii_events=pii,
        gate_blocks=blocks,
        gate_warnings=warnings,
        drift_events=drifts,
        anomaly_events=anomalies,
        remediation_actions=remeds,
        top_blocked_metrics=[dict(r) for r in top_blocked],
        top_pii_traces=[dict(r) for r in top_pii],
        compliance_score=score,
    )

    try:
        db.execute(
            "INSERT INTO otel.gov_compliance_reports "
            "(report_id, report_type, from_ts, to_ts, generated_by, payload) VALUES",
            [(report_id, "summary", from_ts, to_ts, generated_by,
              json.dumps(asdict(report)))],
        )
    except Exception as exc:
        log.warning("compliance_report_save_failed", error=str(exc))

    return report


def get_trace_lineage(db, trace_id: str) -> dict:
    """Return data lineage for a specific trace — agents, PII events, decisions."""
    spans = db.fetch_all(
        "SELECT SpanId AS span_id, SpanName AS span_name, "
        "SpanAttributes AS attributes, Duration AS duration "
        "FROM otel.otel_traces "
        "WHERE TraceId = %(tid)s ORDER BY Timestamp ASC",
        {"tid": trace_id},
    )

    gov_metrics = db.fetch_all(
        "SELECT metric, value, detail, ts FROM otel.gov_metric_snapshots "
        "WHERE trace_id = %(tid)s ORDER BY ts ASC",
        {"tid": trace_id},
    )

    decisions = db.fetch_all(
        "SELECT metric, decision, value, threshold, message "
        "FROM otel.gov_policy_decisions "
        "WHERE trace_id = %(tid)s ORDER BY decision DESC",
        {"tid": trace_id},
    )

    pii_events = [
        {"metric": m["metric"], "detail": m["detail"], "ts": str(m["ts"])}
        for m in gov_metrics if "pii" in str(m.get("metric", ""))
    ]

    agents = list({
        str((s.get("attributes") or {}).get("agent.role", "") or
            (s.get("attributes") or {}).get("agent_name", "") or "unknown")
        for s in spans
    })

    return {
        "trace_id":         trace_id,
        "span_count":       len(spans),
        "agents_involved":  [a for a in agents if a and a != "unknown"],
        "pii_events":       pii_events,
        "policy_decisions": [
            {"metric": d["metric"], "decision": d["decision"],
             "value": d["value"], "threshold": d["threshold"]}
            for d in decisions
        ],
        "spans": [
            {"span_id": s["span_id"], "span_name": s["span_name"],
             "duration_ms": int((s.get("duration") or 0) / 1_000_000)}
            for s in spans[:50]
        ],
    }
