"""Quality gate checker — compares recent eval scores against configured thresholds.

Runs inside the governance watcher cycle every 30s.

Decision lifecycle:
  warn  → audit log only, agent runs
  flag  → audit log, agent runs
  hold  → HITL entry created for operator review (5-min window)
           5 confirmed/expired holds → auto-block → CB failure recorded
  block → HITL entry created, CB failure recorded immediately
           operator approve → CB failure decremented
           operator reject  → CB failure stays (strict mode)

CB failure threshold crossed → CB opens → pre-execution gate hard-blocks agent.
"""

import json
import uuid
from datetime import datetime, timezone

import structlog

log = structlog.get_logger(__name__)

_LOOKBACK_HOURS = 1   # how far back to scan eval_scores each cycle


def _quality_gates_enabled(db) -> bool:
    try:
        row = db.fetch_one(
            "SELECT value FROM otel.gov_threshold_config FINAL "
            "WHERE config_key = 'enforcement.quality_gates_enabled'",
            {},
        )
        if row:
            return float(row.get("value", 0) or 0) >= 1.0
    except Exception:
        pass
    return False


def _get_cfg(db, key: str, default: float) -> float:
    """Read a threshold config value, returning default on any failure."""
    try:
        return db.get_threshold(key, default)
    except Exception:
        return default


def run_quality_gate_checks(db) -> int:
    """Check recent eval scores and process hold timeouts each watcher tick.

    Returns the number of gate decisions written this cycle.
    """
    if not _quality_gates_enabled(db):
        return 0

    # Process timed-out holds first — before writing new decisions
    try:
        _process_hold_timeouts(db)
    except Exception as exc:
        log.warning("hold_timeout_processing_failed", error=str(exc))

    try:
        configs = db.get_quality_gate_configs()
    except Exception as exc:
        log.warning("quality_gate_configs_fetch_failed", error=str(exc))
        return 0

    if not configs:
        return 0

    decisions_written = 0
    for cfg in configs:
        if not int(cfg.get("enabled", 1)):
            continue
        try:
            decisions_written += _check_config(db, cfg)
        except Exception as exc:
            log.warning("quality_gate_check_failed",
                        metric=cfg.get("metric"), error=str(exc))

    return decisions_written


def _process_hold_timeouts(db) -> None:
    """Expire timed-out hold HITL entries and fire auto-block if threshold reached."""
    timeout_secs = int(_get_cfg(db, "quality_gate.hold_review_timeout_seconds", 300))
    expired = db.expire_timed_out_holds(timeout_secs)
    if not expired:
        return

    # Check threshold for each unique agent that had holds expire
    seen_agents = set()
    for item in expired:
        agent_role = item.get("agent_role", "")
        if agent_role and agent_role not in seen_agents:
            seen_agents.add(agent_role)
            _check_hold_threshold(db, agent_role, trigger="timeout")


def _check_hold_threshold(db, agent_role: str, trigger: str = "reject") -> None:
    """If confirmed/expired hold count >= threshold, fire auto-block and record CB failure."""
    threshold = int(_get_cfg(db, "quality_gate.hold_to_block_threshold", 5))
    hold_count = db.get_hold_count(agent_role, hours=24)

    if hold_count < threshold:
        log.info("hold_threshold_not_reached",
                 agent_role=agent_role, hold_count=hold_count, threshold=threshold)
        return

    # Dedup: only fire when hold_count has crossed a new multiple of threshold.
    # score column stores the hold_count at the time of the last auto-block, so
    # we fire at 5, 10, 15, ... — not on every hold reject after threshold is crossed.
    last_block = db.fetch_one(
        "SELECT max(score) AS last_score FROM otel.gov_quality_gate_decisions "
        "WHERE agent_role = %(role)s AND metric = 'hold_accumulation'",
        {"role": agent_role},
    )
    last_score = int(float(last_block.get("last_score") or 0)) if last_block else 0
    # How many full threshold increments have been reached by each count
    if hold_count // threshold <= last_score // threshold:
        log.info("hold_auto_block_dedup",
                 agent_role=agent_role, hold_count=hold_count,
                 last_score=last_score, threshold=threshold)
        return

    reason = (
        f"auto_block: {hold_count} quality gate holds "
        f"confirmed/expired (threshold={threshold}, trigger={trigger})"
    )
    log.warning("hold_threshold_reached_auto_block",
                agent_role=agent_role, hold_count=hold_count, threshold=threshold)

    # Write a synthetic block decision so the pre-execution gate escalates correctly
    decision_id = str(uuid.uuid4())
    db.save_quality_gate_decision(
        decision_id=decision_id,
        trace_id="",
        run_id="",
        agent_role=agent_role,
        metric="hold_accumulation",
        score=float(hold_count),
        threshold=float(threshold),
        action="block",
    )

    # Create HITL entry so operator can override (approve = decrement CB failure)
    _create_block_hitl_entry(db, decision_id, "", "", agent_role,
                             "hold_accumulation", float(hold_count), float(threshold), reason)

    # Record CB failure immediately (strict: approve later can decrement)
    db.record_quality_gate_cb_failure(agent_role, reason)


def _check_config(db, cfg: dict) -> int:
    """Evaluate one gate config against recent eval scores. Returns decisions written."""
    metric     = cfg.get("metric", "")
    threshold  = float(cfg.get("threshold", 0.0))
    action     = str(cfg.get("action", "flag"))
    agent_role = str(cfg.get("agent_role", "*"))

    agent_filter = ""
    params: dict = {
        "metric":    metric,
        "threshold": threshold,
        "hours":     _LOOKBACK_HOURS,
    }
    if agent_role != "*":
        agent_filter = "AND t.agent_role = %(role)s"
        params["role"] = agent_role

    try:
        rows = db.fetch_all(
            f"""
            SELECT
                es.trace_id  AS trace_id,
                es.run_id    AS run_id,
                es.metric,
                es.score,
                COALESCE(NULLIF(t.agent_role, ''), NULLIF(pe.agent_name, ''), '') AS agent_name,
                pe.prompt_text
            FROM otel.eval_scores AS es
            LEFT JOIN (
                SELECT trace_id, agent_name, prompt_text
                FROM otel.prompt_evals
                LIMIT 1 BY trace_id
            ) AS pe ON pe.trace_id = es.trace_id
            LEFT JOIN (
                SELECT TraceId, SpanAttributes['agent.role'] AS agent_role
                FROM otel.otel_traces
                WHERE SpanName = 'agent.task' AND SpanAttributes['agent.role'] != ''
                LIMIT 1 BY TraceId
            ) t ON es.trace_id = t.TraceId
            WHERE es.metric = %(metric)s
              AND es.score < %(threshold)s
              AND es.evaluated_at >= now() - INTERVAL %(hours)s HOUR
              {agent_filter}
            ORDER BY es.evaluated_at DESC
            LIMIT 200
            """,
            params,
        )
    except Exception as exc:
        log.warning("quality_gate_score_query_failed", metric=metric, error=str(exc))
        return 0

    written = 0
    for row in rows:
        trace_id    = str(row.get("trace_id", ""))
        run_id      = str(row.get("run_id", "") or "")
        score       = float(row.get("score", 0.0))
        prompt_text = str(row.get("prompt_text", "") or "")
        raw_role    = str(row.get("agent_name", "") or "")
        role        = raw_role if raw_role and raw_role != "*" else agent_role

        # Skip if we already have a decision for this trace + metric
        existing = db.fetch_one(
            "SELECT decision_id FROM otel.gov_quality_gate_decisions "
            "WHERE trace_id = %(tid)s AND metric = %(metric)s LIMIT 1",
            {"tid": trace_id, "metric": metric},
        )
        if existing:
            continue

        decision_id = str(uuid.uuid4())
        db.save_quality_gate_decision(
            decision_id=decision_id,
            trace_id=trace_id,
            run_id=run_id,
            agent_role=role,
            metric=metric,
            score=score,
            threshold=threshold,
            action=action,
        )
        written += 1

        if action == "hold":
            _create_hold_hitl_entry(db, decision_id, trace_id, run_id,
                                    role, metric, score, threshold, prompt_text)

        elif action == "block":
            reason = f"quality_gate:{metric} score={score:.3f} below threshold={threshold}"
            _create_block_hitl_entry(db, decision_id, trace_id, run_id,
                                     role, metric, score, threshold, reason)
            # Immediate CB failure on block — operator approve can decrement
            db.record_quality_gate_cb_failure(role, reason)

        log.info("quality_gate_decision",
                 trace_id=trace_id, metric=metric,
                 score=round(score, 3), threshold=threshold,
                 action=action, agent_role=role)

    return written


def _create_hold_hitl_entry(db, decision_id, trace_id, run_id,
                             agent_role, metric, score, threshold, prompt_text=""):
    try:
        ctx: dict = {
            "metric":    metric,
            "score":     round(score, 4),
            "threshold": threshold,
            "reason":    f"{metric} score {score:.3f} below threshold {threshold}",
        }
        if prompt_text:
            ctx["query"] = prompt_text[:500]
        db.save_gov_hitl_request(
            trace_id=trace_id,
            span_id="",
            run_id=run_id,
            risk_tier="high",
            action_type="quality_gate_hold",
            payload=json.dumps({
                "agent_role":               agent_role,
                "context":                  ctx,
                "escalation_reasons":       [f"quality_gate:{metric}"],
                "quality_gate_decision_id": decision_id,
            }),
        )
    except Exception as exc:
        log.warning("quality_gate_hold_hitl_create_failed",
                    trace_id=trace_id, metric=metric, error=str(exc))


def _create_block_hitl_entry(db, decision_id, trace_id, run_id,
                              agent_role, metric, score, threshold, reason):
    try:
        db.save_gov_hitl_request(
            trace_id=trace_id,
            span_id="",
            run_id=run_id,
            risk_tier="critical",
            action_type="quality_gate_block",
            payload=json.dumps({
                "agent_role":               agent_role,
                "context": {
                    "metric":    metric,
                    "score":     round(float(score), 4),
                    "threshold": float(threshold),
                    "reason":    reason,
                },
                "escalation_reasons":       [f"quality_gate:{metric}"],
                "quality_gate_decision_id": decision_id,
            }),
        )
    except Exception as exc:
        log.warning("quality_gate_block_hitl_create_failed",
                    trace_id=trace_id, metric=metric, error=str(exc))
