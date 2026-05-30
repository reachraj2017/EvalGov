"""Agent Behavior & Consistency checks — Category 7.

Detects behavioral anomalies including input/output inconsistency, scope
violations, persona drift, out-of-distribution inputs, and statistical
behavioral drift across metrics.

Key public API:
  run_behavior_checks(db, trace_id, run_id, agent_role, spans) → BehaviorResult
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional

import structlog

from anomaly_detector import check_anomaly

log = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class BehaviorResult:
    trace_id:               str
    agent_role:             str
    consistency_score:      float         # 0.0–1.0
    scope_violations:       int
    violated_tools:         list[str]     = field(default_factory=list)
    persona_adherence_score: float        = -1.0  # -1.0 if no persona config
    ood_detected:           bool          = False
    ood_input_tokens:       int           = 0
    behavioral_drift_score: float         = 0.0   # 0.0–1.0


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _sha256_hex(text: str) -> str:
    """Return the lowercase hex SHA-256 digest of text."""
    return hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()


def _extract_tool_name(span: dict) -> Optional[str]:
    """Extract tool name from a tool_call span."""
    attrs = span.get("attributes") or {}
    for key in ("tool.name", "tool_name", "gen_ai.tool.name", "agent.tool"):
        val = attrs.get(key)
        if val:
            return str(val).strip()
    # Fall back to span_name suffix like "agent.tool_call:tool_name"
    span_name = str(span.get("span_name", "") or "")
    if ":" in span_name:
        return span_name.split(":", 1)[1].strip()
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Sub-checks
# ──────────────────────────────────────────────────────────────────────────────

def _check_input_consistency(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    task_spans: list[dict],
    consistency_decrement: float = 0.2,
) -> float:
    """Compute a consistency score by comparing output hashes for identical inputs.

    For each task span with task.input, we hash the normalised input and look
    up prior output hashes stored in gov_metric_snapshots.  If multiple unique
    outputs have been observed for the same input, the score decreases.

    Returns:
        consistency_score in [0.0, 1.0].
    """
    if not task_spans:
        return 1.0

    per_span_scores: list[float] = []

    for span in task_spans:
        attrs   = span.get("attributes") or {}
        span_id = span.get("span_id", "")

        task_input  = str(attrs.get("task.input",  "") or
                          attrs.get("gen_ai.prompt", "") or "").strip()
        task_output = str(attrs.get("task.output", "") or
                          attrs.get("gen_ai.completion", "") or "").strip()

        if not task_input:
            continue

        input_hash  = _sha256_hex(task_input.lower())
        output_hash = _sha256_hex(task_output) if task_output else ""

        # Store the input hash as a metric (value is a numeric fingerprint for
        # time-series storage; the full hash is in the detail JSON)
        try:
            db.save_gov_metric(
                trace_id, span_id, run_id,
                "behavior.input_hash",
                float(int(input_hash[:8], 16) % 1_000_000),
                json.dumps({"hash": input_hash, "output_hash": output_hash}),
            )
        except Exception as exc:
            log.warning("behavior_input_hash_save_failed",
                        span_id=span_id, error=str(exc))

        if not output_hash:
            per_span_scores.append(1.0)
            continue

        # Query prior output hashes seen for this input_hash
        try:
            prior_rows = db.fetch_all(
                "SELECT detail FROM otel.gov_metric_snapshots "
                "WHERE metric = 'behavior.input_hash' "
                "  AND trace_id != %(tid)s "
                "  AND detail LIKE %(pattern)s "
                "LIMIT 200",
                {"tid": trace_id, "pattern": f'%"hash": "{input_hash}"%'},
            )
        except Exception as exc:
            log.warning("behavior_prior_hash_query_failed",
                        span_id=span_id, error=str(exc))
            prior_rows = []

        prior_output_hashes: set[str] = set()
        for row in prior_rows:
            try:
                detail_obj = json.loads(row.get("detail", "{}") or "{}")
                oh = detail_obj.get("output_hash", "")
                if oh:
                    prior_output_hashes.add(oh)
            except (json.JSONDecodeError, TypeError):
                pass

        # Include the current output in the unique set
        prior_output_hashes.add(output_hash)
        unique_outputs = len(prior_output_hashes)

        # Consistency decreases as more unique outputs are seen:
        # 1 unique → 1.0, 2 → 0.8, 3 → 0.6, etc. (floor at 0.0)
        span_score = max(0.0, 1.0 - (unique_outputs - 1) * consistency_decrement)
        per_span_scores.append(span_score)

    if not per_span_scores:
        return 1.0
    return sum(per_span_scores) / len(per_span_scores)


def _check_scope_violations(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    spans:      list[dict],
) -> tuple[int, list[str]]:
    """Check tool_call spans against gov_agent_tool_whitelist.

    Returns:
        (violation_count, violated_tool_names)
    """
    # Load the whitelist for this agent_role
    whitelist: list[str] = []
    try:
        rows = db.fetch_all(
            "SELECT tool_name FROM otel.gov_agent_tool_whitelist FINAL "
            "WHERE agent_role = %(role)s AND allowed = 1",
            {"role": agent_role},
        )
        whitelist = [r["tool_name"] for r in rows if r.get("tool_name")]
    except Exception as exc:
        log.warning("behavior_tool_whitelist_query_failed",
                    agent_role=agent_role, error=str(exc))

    if not whitelist:
        # No whitelist configured — cannot assess scope
        return 0, []

    violations     = 0
    violated_tools: list[str] = []

    for span in spans:
        span_name = str(span.get("span_name", "") or "")
        if "tool" not in span_name.lower():
            continue

        span_id   = span.get("span_id", "")
        tool_name = _extract_tool_name(span)
        if not tool_name:
            continue

        if tool_name not in whitelist:
            violations += 1
            if tool_name not in violated_tools:
                violated_tools.append(tool_name)
            try:
                db.execute(
                    "INSERT INTO otel.gov_behavior_events "
                    "(trace_id, span_id, run_id, agent_role, event_type, detail, score) VALUES",
                    [(trace_id, span_id, run_id, agent_role,
                      "scope_violation",
                      json.dumps({"tool": tool_name}),
                      1.0)],
                )
            except Exception as exc:
                log.warning("behavior_scope_violation_write_failed",
                            span_id=span_id, tool=tool_name, error=str(exc))
            log.warning("behavior_scope_violation",
                        trace_id=trace_id, span_id=span_id,
                        tool=tool_name, agent_role=agent_role)

    return violations, violated_tools


def _check_persona_adherence(
    db,
    agent_role: str,
    task_spans: list[dict],
    min_output_len: int = 100,
) -> float:
    """Check whether task outputs stay on-topic per the agent's persona config.

    Returns:
        Adherence score in [0.0, 1.0], or -1.0 if no persona config exists.
    """
    authorized_topics: list[str] = []
    try:
        persona_row = db.fetch_one(
            "SELECT authorized_topics FROM otel.gov_persona_config FINAL "
            "WHERE agent_role = %(role)s",
            {"role": agent_role},
        )
        if persona_row:
            raw_topics = persona_row.get("authorized_topics") or []
            if isinstance(raw_topics, str):
                try:
                    raw_topics = json.loads(raw_topics)
                except (json.JSONDecodeError, TypeError):
                    raw_topics = [t.strip() for t in raw_topics.split(",") if t.strip()]
            authorized_topics = [str(t).lower() for t in raw_topics if t]
    except Exception as exc:
        log.warning("behavior_persona_config_query_failed",
                    agent_role=agent_role, error=str(exc))

    if not authorized_topics:
        # No persona config — cannot assess adherence
        return -1.0

    on_topic_count  = 0
    assessed_count  = 0

    for span in task_spans:
        attrs       = span.get("attributes") or {}
        task_output = str(attrs.get("task.output", "") or
                          attrs.get("gen_ai.completion", "") or "").strip()

        if len(task_output) <= min_output_len:
            # Too short to meaningfully assess topic adherence
            continue

        assessed_count += 1
        output_lower    = task_output.lower()
        if any(topic in output_lower for topic in authorized_topics):
            on_topic_count += 1
        else:
            log.warning("behavior_persona_drift_suspected",
                        agent_role=agent_role,
                        span_id=span.get("span_id", ""),
                        output_snippet=task_output[:120])

    if assessed_count == 0:
        return 1.0  # Nothing long enough to assess — assume adherent
    return on_topic_count / assessed_count


def _check_ood(
    db,
    agent_role: str,
    task_spans: list[dict],
    zscore_multiplier: float = 3.0,
) -> tuple[bool, int]:
    """Heuristic OOD detection based on input token count vs. baseline.

    Compares the total token count of each task.input against the
    'input_token_count' baseline in gov_agent_baselines.  Flags the trace
    as OOD if any single span's input exceeds mean + 3 * stddev.

    Returns:
        (ood_detected, max_input_tokens_seen)
    """
    max_tokens    = 0
    ood_detected  = False

    try:
        baseline = db.fetch_one(
            "SELECT mean, stddev, sample_count FROM otel.gov_agent_baselines FINAL "
            "WHERE agent_role = %(role)s AND metric = 'input_token_count'",
            {"role": agent_role},
        )
        if not baseline or int(baseline.get("sample_count", 0) or 0) < 5:
            baseline = db.fetch_one(
                "SELECT mean, stddev, sample_count FROM otel.gov_agent_baselines FINAL "
                "WHERE agent_role = '__global__' AND metric = 'input_token_count'",
            )
    except Exception as exc:
        log.warning("behavior_ood_baseline_query_failed",
                    agent_role=agent_role, error=str(exc))
        baseline = None

    if not baseline or int(baseline.get("sample_count", 0) or 0) < 5:
        # No baseline — cannot detect OOD
        for span in task_spans:
            attrs      = span.get("attributes") or {}
            task_input = str(attrs.get("task.input", "") or "")
            tokens     = len(task_input.split())
            if tokens > max_tokens:
                max_tokens = tokens
        return False, max_tokens

    mean   = float(baseline.get("mean",   0) or 0)
    stddev = float(baseline.get("stddev", 0) or 0)

    if stddev < 1e-9:
        return False, max_tokens

    threshold = mean + zscore_multiplier * stddev

    for span in task_spans:
        attrs      = span.get("attributes") or {}
        task_input = str(attrs.get("task.input", "") or
                         attrs.get("gen_ai.prompt", "") or "")
        tokens     = len(task_input.split())
        if tokens > max_tokens:
            max_tokens = tokens
        if tokens > threshold:
            ood_detected = True
            log.warning("behavior_ood_input_detected",
                        agent_role=agent_role,
                        span_id=span.get("span_id", ""),
                        token_count=tokens,
                        threshold=round(threshold, 1))

    return ood_detected, max_tokens


def _compute_drift_score(
    db,
    agent_role: str,
    metrics:    dict[str, float],
    thresholds: dict = None,
) -> float:
    """Compute a combined behavioral drift score using anomaly baselines.

    Calls check_anomaly for each supplied metric, collects z-scores, and
    returns max(z_scores) / 4.0 clamped to [0.0, 1.0].

    Args:
        db:         GovernanceDB instance.
        agent_role: Agent role for per-agent baseline lookup.
        metrics:    Dict of metric_name → observed_value.

    Returns:
        drift_score in [0.0, 1.0].
    """
    z_scores: list[float] = []

    for metric, value in metrics.items():
        try:
            result = check_anomaly(db, agent_role, metric, value, thresholds=thresholds)
            if result and result.is_anomaly:
                z_scores.append(result.z_score)
        except Exception as exc:
            log.warning("behavior_drift_anomaly_check_failed",
                        metric=metric, error=str(exc))

    if not z_scores:
        return 0.0
    return min(1.0, max(z_scores) / 4.0)


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

def run_behavior_checks(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    spans:      list[dict],
    thresholds: dict = None,
) -> BehaviorResult:
    """Run all agent behavior & consistency checks for the supplied spans.

    Args:
        db:         GovernanceDB instance.
        trace_id:   OTel trace ID.
        run_id:     Governance run ID.
        agent_role: Role of the agent being evaluated.
        spans:      List of span dicts as returned by db.get_spans_for_trace().
        thresholds: Optional dict of threshold overrides keyed by threshold name.

    Returns:
        BehaviorResult dataclass with all check results.
    """
    t               = thresholds or {}
    _consistency_dec = float(t.get("behavior.consistency_decrement",   0.2))
    _min_output_len  = int(t.get("behavior.min_output_length",          100))
    _ood_zscore_mult = float(t.get("behavior.ood_zscore_multiplier",    3.0))

    task_spans = [s for s in spans if s.get("span_name") == "agent.task"]

    # ── 1. Input/output hash consistency ─────────────────────────────────
    consistency_score = 1.0
    try:
        consistency_score = _check_input_consistency(
            db, trace_id, run_id, agent_role, task_spans,
            consistency_decrement=_consistency_dec,
        )
    except Exception as exc:
        log.warning("behavior_consistency_check_failed",
                    trace_id=trace_id, error=str(exc))

    # ── 2. Scope violation check ──────────────────────────────────────────
    scope_violations = 0
    violated_tools:  list[str] = []
    try:
        scope_violations, violated_tools = _check_scope_violations(
            db, trace_id, run_id, agent_role, spans
        )
    except Exception as exc:
        log.warning("behavior_scope_check_failed",
                    trace_id=trace_id, error=str(exc))

    # ── 3. Persona adherence ──────────────────────────────────────────────
    persona_adherence_score = -1.0
    try:
        persona_adherence_score = _check_persona_adherence(
            db, agent_role, task_spans,
            min_output_len=_min_output_len,
        )
    except Exception as exc:
        log.warning("behavior_persona_check_failed",
                    trace_id=trace_id, error=str(exc))

    # ── 4. OOD detection ─────────────────────────────────────────────────
    ood_detected     = False
    ood_input_tokens = 0
    try:
        ood_detected, ood_input_tokens = _check_ood(
            db, agent_role, task_spans,
            zscore_multiplier=_ood_zscore_mult,
        )
    except Exception as exc:
        log.warning("behavior_ood_check_failed",
                    trace_id=trace_id, error=str(exc))

    # ── 5. Behavioral drift via anomaly baselines ─────────────────────────
    behavioral_drift_score = 0.0
    try:
        drift_metrics: dict[str, float] = {
            "behavior.consistency_score":      consistency_score,
            "behavior.scope_violation_rate":   (
                scope_violations / len(spans) if spans else 0.0
            ),
        }
        if persona_adherence_score >= 0.0:
            drift_metrics["behavior.persona_adherence_score"] = persona_adherence_score
        if ood_input_tokens > 0:
            drift_metrics["input_token_count"] = float(ood_input_tokens)

        behavioral_drift_score = _compute_drift_score(
            db, agent_role, drift_metrics, thresholds=thresholds,
        )
    except Exception as exc:
        log.warning("behavior_drift_score_failed",
                    trace_id=trace_id, error=str(exc))

    # ── Persist metric snapshots ──────────────────────────────────────────
    scope_violation_rate = scope_violations / len(spans) if spans else 0.0

    try:
        db.save_gov_metric(
            trace_id, "", run_id,
            "consistency_score", consistency_score, ""
        )
    except Exception as exc:
        log.warning("behavior_metric_save_failed",
                    metric="consistency_score", error=str(exc))

    try:
        db.save_gov_metric(
            trace_id, "", run_id,
            "scope_violation_rate", scope_violation_rate,
            json.dumps({"violated_tools": violated_tools}),
        )
    except Exception as exc:
        log.warning("behavior_metric_save_failed",
                    metric="scope_violation_rate", error=str(exc))

    try:
        db.save_gov_metric(
            trace_id, "", run_id,
            "persona_adherence_score", persona_adherence_score, ""
        )
    except Exception as exc:
        log.warning("behavior_metric_save_failed",
                    metric="persona_adherence_score", error=str(exc))

    try:
        db.save_gov_metric(
            trace_id, "", run_id,
            "behavioral_drift_score", behavioral_drift_score, ""
        )
    except Exception as exc:
        log.warning("behavior_metric_save_failed",
                    metric="behavioral_drift_score", error=str(exc))

    result = BehaviorResult(
        trace_id=trace_id,
        agent_role=agent_role,
        consistency_score=round(consistency_score, 4),
        scope_violations=scope_violations,
        violated_tools=violated_tools,
        persona_adherence_score=round(persona_adherence_score, 4),
        ood_detected=ood_detected,
        ood_input_tokens=ood_input_tokens,
        behavioral_drift_score=round(behavioral_drift_score, 4),
    )

    log.info(
        "behavior_checks_complete",
        trace_id=trace_id,
        agent_role=agent_role,
        consistency_score=result.consistency_score,
        scope_violations=scope_violations,
        violated_tools=violated_tools,
        persona_adherence_score=result.persona_adherence_score,
        ood_detected=ood_detected,
        ood_input_tokens=ood_input_tokens,
        behavioral_drift_score=result.behavioral_drift_score,
    )

    return result
