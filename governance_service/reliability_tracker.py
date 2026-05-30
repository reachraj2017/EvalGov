"""SRE Reliability metrics — Category 6: Reliability, SRE & Error Budgets.

Computes SLO/SLA metrics by querying otel.otel_traces for the agent_role
over recent history.  The current trace triggers computation but metrics
aggregate over the configured window.

Key metrics computed:
  error_rate              — fraction of spans with STATUS_CODE_ERROR
  p50/p95/p99_ms          — latency percentiles (pure-Python, no numpy)
  availability            — 1.0 - error_rate (proxy; heartbeat not used here)
  error_budget_consumed   — fraction of error budget consumed over slo_window_days
  mttr_minutes            — mean time to recover from resolved incidents
  graceful_degradation_rate — fraction of errors that used fallback / retry

Public API:
  seed_slo_config(db)                                           → None
  check_reliability(db, agent_role, trace_id, run_id,
                    window_hours=1)                             → ReliabilityResult
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Default SLO targets used when gov_slo_config has no row for an agent_role
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_TARGET_AVAILABILITY = 0.999
_DEFAULT_ERROR_BUDGET_PCT    = 0.001   # 0.1 % errors allowed
_DEFAULT_SLO_WINDOW_DAYS     = 30
_DEFAULT_P95_TARGET_MS       = 2000.0
_DEFAULT_P99_TARGET_MS       = 5000.0


# ──────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class ReliabilityResult:
    trace_id:                 str
    agent_role:               str
    error_rate:               float
    p50_ms:                   float
    p95_ms:                   float
    p99_ms:                   float
    availability:             float
    error_budget_consumed:    float
    slo_window_days:          int
    error_budget_pct:         float
    p95_target_ms:            float
    p99_target_ms:            float
    mttr_minutes:             float   # -1.0 if no resolved incidents
    graceful_degradation_rate: float
    total_spans:              int
    error_spans:              int
    sla_breach:               bool    # True if availability < target_availability
    p95_breach:               bool    # True if p95_ms > p95_target_ms


# ──────────────────────────────────────────────────────────────────────────────
# Pure-Python percentile helper
# ──────────────────────────────────────────────────────────────────────────────

def _percentile(sorted_values: list[float], pct: float) -> float:
    """Return the pct-th percentile of a sorted list (nearest-rank method).

    Args:
        sorted_values: Pre-sorted list of floats.
        pct:           Percentile as a value in [0, 100].

    Returns:
        The percentile value, or 0.0 for an empty list.
    """
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    # Nearest-rank: ceil(pct/100 * n), clamped to [1, n]
    rank = max(1, min(n, int(pct / 100.0 * n + 0.9999)))
    return sorted_values[rank - 1]


# ──────────────────────────────────────────────────────────────────────────────
# Seed helper
# ──────────────────────────────────────────────────────────────────────────────

def seed_slo_config(db) -> None:
    """Insert a default '__default__' SLO row if gov_slo_config is empty.

    Safe to call multiple times — does nothing if a row already exists.
    """
    try:
        existing = db.fetch_one(
            "SELECT count() AS cnt FROM otel.gov_slo_config FINAL"
        )
        if existing and int(existing.get("cnt", 0) or 0) > 0:
            return
        db.execute(
            "INSERT INTO otel.gov_slo_config "
            "(agent_role, target_availability, error_budget_pct, "
            "slo_window_days, p95_target_ms, p99_target_ms) VALUES",
            [("__default__",
              _DEFAULT_TARGET_AVAILABILITY,
              _DEFAULT_ERROR_BUDGET_PCT,
              _DEFAULT_SLO_WINDOW_DAYS,
              _DEFAULT_P95_TARGET_MS,
              _DEFAULT_P99_TARGET_MS)],
        )
        log.info("slo_config_seeded", agent_role="__default__")
    except Exception as exc:
        log.warning("slo_config_seed_failed", error=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

def check_reliability(
    db,
    agent_role:   str,
    trace_id:     str,
    run_id:       str,
    window_hours: int = 1,
) -> ReliabilityResult:
    """Compute SRE reliability metrics for agent_role over window_hours.

    The current trace (trace_id) triggers the computation, but all
    metrics aggregate over recent history for this agent_role.

    Args:
        db:           GovernanceDB instance.
        agent_role:   Role string matched against SpanAttributes['agent.role'].
        trace_id:     OTel trace ID of the triggering trace.
        run_id:       Governance run ID.
        window_hours: How many hours back to scan for agent.task spans.

    Returns:
        ReliabilityResult dataclass with all computed SRE metrics.
    """
    # Track window boundaries for the gov_reliability_metrics insert
    window_end   = datetime.now(timezone.utc)

    # ── 1. Query agent.task spans for this role in the window ─────────────
    rows: list[dict] = []
    try:
        rows = db.fetch_all(
            "SELECT Duration, StatusCode FROM otel.otel_traces "
            "WHERE SpanName = 'agent.task' "
            "  AND SpanAttributes['agent.role'] = %(role)s "
            "  AND Timestamp >= now() - INTERVAL %(hours)s HOUR",
            {"role": agent_role, "hours": int(window_hours)},
        )
    except Exception as exc:
        log.warning("reliability_span_query_failed",
                    agent_role=agent_role, error=str(exc))

    if not rows:
        # Retry without agent.role filter — some spans may not have it set
        try:
            rows = db.fetch_all(
                "SELECT Duration, StatusCode FROM otel.otel_traces "
                "WHERE SpanName = 'agent.task' "
                "  AND Timestamp >= now() - INTERVAL %(hours)s HOUR",
                {"hours": int(window_hours)},
            )
        except Exception as exc:
            log.warning("reliability_span_query_fallback_failed",
                        agent_role=agent_role, error=str(exc))

    # ── 2. Compute basic metrics ──────────────────────────────────────────
    total_count = len(rows)
    error_count = sum(
        1 for r in rows
        if str(r.get("StatusCode", "") or "") == "STATUS_CODE_ERROR"
    )

    error_rate   = error_count / total_count if total_count > 0 else 0.0
    availability = 1.0 - error_rate

    durations_ms = sorted(
        float(r["Duration"]) / 1_000_000
        for r in rows
        if r.get("Duration") is not None
    )

    p50_ms = _percentile(durations_ms, 50.0)
    p95_ms = _percentile(durations_ms, 95.0)
    p99_ms = _percentile(durations_ms, 99.0)

    # ── 3. Query SLO config for this agent_role ───────────────────────────
    target_availability = _DEFAULT_TARGET_AVAILABILITY
    error_budget_pct    = _DEFAULT_ERROR_BUDGET_PCT
    slo_window_days     = _DEFAULT_SLO_WINDOW_DAYS
    p95_target_ms       = _DEFAULT_P95_TARGET_MS
    p99_target_ms       = _DEFAULT_P99_TARGET_MS

    try:
        slo_row = db.fetch_one(
            "SELECT target_availability, error_budget_pct, slo_window_days, "
            "p95_target_ms, p99_target_ms "
            "FROM otel.gov_slo_config FINAL WHERE agent_role = %(role)s",
            {"role": agent_role},
        )
        if not slo_row:
            # Fall back to '__default__' row
            slo_row = db.fetch_one(
                "SELECT target_availability, error_budget_pct, slo_window_days, "
                "p95_target_ms, p99_target_ms "
                "FROM otel.gov_slo_config FINAL WHERE agent_role = '__default__'",
            )
        if slo_row:
            target_availability = float(slo_row.get("target_availability",
                                                     _DEFAULT_TARGET_AVAILABILITY)
                                        or _DEFAULT_TARGET_AVAILABILITY)
            error_budget_pct    = float(slo_row.get("error_budget_pct",
                                                     _DEFAULT_ERROR_BUDGET_PCT)
                                        or _DEFAULT_ERROR_BUDGET_PCT)
            slo_window_days     = int(slo_row.get("slo_window_days",
                                                   _DEFAULT_SLO_WINDOW_DAYS)
                                      or _DEFAULT_SLO_WINDOW_DAYS)
            p95_target_ms       = float(slo_row.get("p95_target_ms",
                                                     _DEFAULT_P95_TARGET_MS)
                                        or _DEFAULT_P95_TARGET_MS)
            p99_target_ms       = float(slo_row.get("p99_target_ms",
                                                     _DEFAULT_P99_TARGET_MS)
                                        or _DEFAULT_P99_TARGET_MS)
    except Exception as exc:
        log.warning("slo_config_query_failed", agent_role=agent_role, error=str(exc))

    # ── 4. Compute error budget consumed over slo_window_days ────────────
    error_budget_consumed = 0.0
    try:
        window_row = db.fetch_one(
            "SELECT count() AS total, "
            "countIf(StatusCode = 'STATUS_CODE_ERROR') AS errors "
            "FROM otel.otel_traces "
            "WHERE SpanName = 'agent.task' "
            "  AND SpanAttributes['agent.role'] = %(role)s "
            "  AND Timestamp >= now() - INTERVAL %(days)s DAY",
            {"role": agent_role, "days": int(slo_window_days)},
        )
        if window_row and int(window_row.get("total", 0) or 0) > 0:
            window_total  = int(window_row["total"])
            window_errors = int(window_row.get("errors", 0) or 0)
            actual_error_rate = window_errors / window_total
            if error_budget_pct > 0:
                error_budget_consumed = actual_error_rate / error_budget_pct
            else:
                error_budget_consumed = 0.0
        # Clamp to [0.0, 10.0] to avoid infinity on zero budget
        error_budget_consumed = max(0.0, min(10.0, error_budget_consumed))
    except Exception as exc:
        log.warning("error_budget_query_failed", agent_role=agent_role, error=str(exc))

    # ── 5. Query MTTR from resolved incidents ────────────────────────────
    mttr_minutes = -1.0
    try:
        incident_rows = db.fetch_all(
            "SELECT toUnixTimestamp(resolved_at) - toUnixTimestamp(opened_at) "
            "  AS mttr_secs "
            "FROM otel.gov_incidents "
            "WHERE agent_role = %(role)s AND status = 'resolved' "
            "  AND resolved_at > '1970-01-02' "
            "  AND opened_at >= now() - INTERVAL 30 DAY",
            {"role": agent_role},
        )
        if incident_rows:
            secs_list = [
                float(r.get("mttr_secs", 0) or 0)
                for r in incident_rows
                if r.get("mttr_secs") is not None
            ]
            if secs_list:
                mttr_minutes = sum(secs_list) / len(secs_list) / 60.0
    except Exception as exc:
        log.warning("mttr_query_failed", agent_role=agent_role, error=str(exc))

    # ── 6. Graceful degradation rate ─────────────────────────────────────
    graceful_count  = 0
    degraded_count  = error_count   # spans that errored
    try:
        graceful_row = db.fetch_one(
            "SELECT count() AS graceful FROM otel.otel_traces "
            "WHERE SpanName = 'agent.task' "
            "  AND SpanAttributes['agent.role'] = %(role)s "
            "  AND Timestamp >= now() - INTERVAL %(hours)s HOUR "
            "  AND StatusCode = 'STATUS_CODE_ERROR' "
            "  AND (SpanAttributes['fallback.used'] = 'true' "
            "       OR toUInt32OrZero(SpanAttributes['retry.count']) > 0)",
            {"role": agent_role, "hours": int(window_hours)},
        )
        if graceful_row:
            graceful_count = int(graceful_row.get("graceful", 0) or 0)
    except Exception as exc:
        log.warning("graceful_degradation_query_failed",
                    agent_role=agent_role, error=str(exc))

    graceful_degradation_rate = (
        graceful_count / error_count if error_count > 0 else 1.0
    )

    # ── Derive breach flags ───────────────────────────────────────────────
    sla_breach = availability < target_availability
    p95_breach = p95_ms > p95_target_ms and total_count > 0

    # ── Persist metric snapshots ──────────────────────────────────────────
    try:
        db.save_gov_metric(trace_id, "", run_id, "error_rate", error_rate, "")
    except Exception as exc:
        log.warning("reliability_metric_save_failed",
                    metric="error_rate", error=str(exc))

    try:
        db.save_gov_metric(trace_id, "", run_id, "p95_latency_ms", p95_ms, "")
    except Exception as exc:
        log.warning("reliability_metric_save_failed",
                    metric="p95_latency_ms", error=str(exc))

    try:
        db.save_gov_metric(trace_id, "", run_id, "availability", availability, "")
    except Exception as exc:
        log.warning("reliability_metric_save_failed",
                    metric="availability", error=str(exc))

    try:
        db.save_gov_metric(
            trace_id, "", run_id,
            "error_budget_consumed",
            error_budget_consumed,
            json.dumps({
                "slo_window_days":   slo_window_days,
                "error_budget_pct":  error_budget_pct,
            }),
        )
    except Exception as exc:
        log.warning("reliability_metric_save_failed",
                    metric="error_budget_consumed", error=str(exc))

    # ── Write gov_reliability_metrics row ────────────────────────────────
    try:
        # Approximate window_start from window_end minus window_hours
        from datetime import timedelta
        window_start = window_end - timedelta(hours=window_hours)
        db.execute(
            "INSERT INTO otel.gov_reliability_metrics "
            "(agent_role, window_start, window_end, "
            "total_spans, error_spans, error_rate, p50_ms, p95_ms, p99_ms, "
            "availability, error_budget_consumed, graceful_count, degraded_count) VALUES",
            [(agent_role,
              window_start, window_end,
              total_count, error_count, error_rate,
              p50_ms, p95_ms, p99_ms,
              availability, error_budget_consumed,
              graceful_count, degraded_count)],
        )
    except Exception as exc:
        log.warning("reliability_metrics_insert_failed",
                    agent_role=agent_role, error=str(exc))

    result = ReliabilityResult(
        trace_id=trace_id,
        agent_role=agent_role,
        error_rate=round(error_rate, 6),
        p50_ms=round(p50_ms, 3),
        p95_ms=round(p95_ms, 3),
        p99_ms=round(p99_ms, 3),
        availability=round(availability, 6),
        error_budget_consumed=round(error_budget_consumed, 4),
        slo_window_days=slo_window_days,
        error_budget_pct=error_budget_pct,
        p95_target_ms=p95_target_ms,
        p99_target_ms=p99_target_ms,
        mttr_minutes=round(mttr_minutes, 3),
        graceful_degradation_rate=round(graceful_degradation_rate, 4),
        total_spans=total_count,
        error_spans=error_count,
        sla_breach=sla_breach,
        p95_breach=p95_breach,
    )

    log.info(
        "reliability_check_complete",
        trace_id=trace_id,
        agent_role=agent_role,
        error_rate=result.error_rate,
        availability=result.availability,
        p95_ms=result.p95_ms,
        error_budget_consumed=result.error_budget_consumed,
        sla_breach=sla_breach,
        p95_breach=p95_breach,
        total_spans=total_count,
        error_spans=error_count,
    )

    return result
