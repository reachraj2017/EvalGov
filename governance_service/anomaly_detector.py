"""Statistical anomaly detection using per-agent metric baselines.

Maintains mean + stddev per (agent_role, metric) over a rolling window.
Flags observations that deviate by ≥ Z_THRESHOLD standard deviations.

Severity levels:
  medium   — z ≥ 2.0
  high     — z ≥ 3.0
  critical — z ≥ 4.0
"""

from dataclasses import dataclass
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

Z_MEDIUM   = 2.0
Z_HIGH     = 3.0
Z_CRITICAL = 4.0
MIN_SAMPLES = 5  # baseline must have at least this many samples

MONITORED_METRICS = [
    "pii_leak_rate",
    "prompt_snapshot_coverage",
    "budget_utilization",
    "routing_savings_pct",
    # LLM-judged eval quality metrics — sourced from eval_scores, not gov_metric_snapshots
    "faithfulness",
    "instruction_following",
    "coherence",
    "relevance",
]

# Metrics whose baselines are computed from eval_scores rather than gov_metric_snapshots
EVAL_SCORE_METRICS = {"faithfulness", "instruction_following", "coherence", "relevance"}


@dataclass
class AnomalyResult:
    agent_role:      str
    metric:          str
    observed_value:  float
    baseline_mean:   float
    baseline_stddev: float
    z_score:         float
    severity:        str   # medium | high | critical
    is_anomaly:      bool


def recompute_baselines(db, window_days: int = 7) -> None:
    """Recompute global baselines for all monitored metrics.

    Called on startup and periodically (e.g. daily).
    Stores a single '__global__' row per metric — per-agent baselines
    require more data than most deployments will have.
    Eval score metrics read from eval_scores; operational metrics from gov_metric_snapshots.
    """
    for metric in MONITORED_METRICS:
        try:
            if metric in EVAL_SCORE_METRICS:
                row = db.fetch_one(
                    f"SELECT avg(score) AS mean_val, stddevPop(score) AS stddev_val, "
                    f"count() AS n "
                    f"FROM otel.eval_scores "
                    f"WHERE metric = %(m)s "
                    f"  AND evaluated_at >= now() - INTERVAL {int(window_days)} DAY",
                    {"m": metric},
                )
            else:
                row = db.fetch_one(
                    f"SELECT avg(value) AS mean_val, stddevPop(value) AS stddev_val, "
                    f"count() AS n "
                    f"FROM otel.gov_metric_snapshots "
                    f"WHERE metric = %(m)s AND span_id = '' "
                    f"  AND ts >= now() - INTERVAL {int(window_days)} DAY",
                    {"m": metric},
                )
            if not row or int(row.get("n", 0) or 0) < MIN_SAMPLES:
                continue
            db.execute(
                "INSERT INTO otel.gov_agent_baselines "
                "(agent_role, metric, mean, stddev, sample_count, window_days) VALUES",
                [("__global__", metric,
                  float(row["mean_val"]   or 0),
                  float(row["stddev_val"] or 0),
                  int(row["n"]            or 0),
                  window_days)],
            )
            log.debug("baseline_updated", metric=metric, mean=row["mean_val"],
                      stddev=row["stddev_val"], n=row["n"])
        except Exception as exc:
            log.warning("baseline_recompute_failed", metric=metric, error=str(exc))


def check_anomaly(
    db,
    agent_role: str,
    metric: str,
    observed_value: float,
    thresholds: dict = None,
) -> Optional[AnomalyResult]:
    """Return AnomalyResult if observed_value is anomalous, else None."""
    t          = thresholds or {}
    z_medium   = float(t.get("anomaly.zscore_medium",   Z_MEDIUM))
    z_high     = float(t.get("anomaly.zscore_high",     Z_HIGH))
    z_critical = float(t.get("anomaly.zscore_critical", Z_CRITICAL))
    min_samp   = int(t.get("anomaly.min_samples",       MIN_SAMPLES))
    # Try agent-specific baseline first, fall back to global
    baseline = None
    if agent_role:
        baseline = db.fetch_one(
            "SELECT mean, stddev, sample_count FROM otel.gov_agent_baselines FINAL "
            "WHERE agent_role = %(role)s AND metric = %(metric)s",
            {"role": agent_role, "metric": metric},
        )

    if not baseline or int(baseline.get("sample_count", 0) or 0) < min_samp:
        baseline = db.fetch_one(
            "SELECT mean, stddev, sample_count FROM otel.gov_agent_baselines FINAL "
            "WHERE agent_role = '__global__' AND metric = %(metric)s",
            {"metric": metric},
        )

    if not baseline or int(baseline.get("sample_count", 0) or 0) < min_samp:
        return None

    mean   = float(baseline.get("mean",   0) or 0)
    stddev = float(baseline.get("stddev", 0) or 0)

    if stddev < 1e-9:
        return None

    z = abs(observed_value - mean) / stddev

    if z < z_medium:
        return None

    severity = "critical" if z >= z_critical else ("high" if z >= z_high else "medium")

    return AnomalyResult(
        agent_role=agent_role,
        metric=metric,
        observed_value=observed_value,
        baseline_mean=mean,
        baseline_stddev=stddev,
        z_score=round(z, 3),
        severity=severity,
        is_anomaly=True,
    )


def run_anomaly_checks(
    db,
    trace_id: str,
    run_id: str,
    agent_role: str,
    metrics: dict[str, float],
    thresholds: dict = None,
) -> list[AnomalyResult]:
    """Check all monitored metrics and persist any anomalies found."""
    anomalies: list[AnomalyResult] = []
    for metric in MONITORED_METRICS:
        if metric not in metrics:
            continue
        try:
            result = check_anomaly(db, agent_role, metric, metrics[metric], thresholds=thresholds)
            if result:
                db.execute(
                    "INSERT INTO otel.gov_anomaly_events "
                    "(trace_id, run_id, agent_role, metric, observed_value, "
                    "baseline_mean, baseline_stddev, z_score, severity) VALUES",
                    [(trace_id, run_id, agent_role, metric,
                      result.observed_value, result.baseline_mean,
                      result.baseline_stddev, result.z_score, result.severity)],
                )
                anomalies.append(result)
                log.warning("anomaly_detected", trace_id=trace_id,
                            metric=metric, z_score=result.z_score,
                            severity=result.severity)
        except Exception as exc:
            log.warning("anomaly_check_failed", metric=metric, error=str(exc))
    return anomalies
