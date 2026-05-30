"""Agent Lifecycle & Version Control — Category 11.

Tracks model version pinning, prompt drift rates, eval gate pass rates,
change log detection, and canary/shadow deployment mode detection.

Key public API:
  run_lifecycle_checks(db, trace_id, run_id, agent_role, spans) → LifecycleResult
"""

import json
from dataclasses import dataclass, field

import structlog

log = structlog.get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class LifecycleResult:
    trace_id:               str
    agent_role:             str
    model_versions_seen:    list[str]
    is_pinned:              bool
    pinned_version:         str
    version_drift_detected: bool
    prompt_drift_rate:      float    # rolling 7-day
    eval_gate_pass_rate:    float
    in_canary:              bool
    in_shadow:              bool
    canary_pct:             float
    change_detected:        bool


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _extract_model_versions(spans: list[dict]) -> list[str]:
    """Collect unique model version strings seen across all span attributes."""
    seen: set[str] = set()
    for span in spans:
        attrs = span.get("attributes") or {}
        for key in ("gen_ai.request.model", "model_name", "agent.model_version"):
            val = attrs.get(key)
            if val:
                v = str(val).strip()
                if v:
                    seen.add(v)
    return sorted(seen)


def _detect_deployment_modes(
    spans: list[dict],
) -> tuple[bool, bool, float]:
    """Scan spans for deployment.mode = 'canary' or 'shadow'.

    Returns:
        (in_canary, in_shadow, canary_pct)
    """
    total = len(spans)
    canary_count = 0
    in_shadow    = False

    for span in spans:
        attrs = span.get("attributes") or {}
        mode  = str(attrs.get("deployment.mode", "") or "").lower()
        if mode == "canary":
            canary_count += 1
        elif mode == "shadow":
            in_shadow = True

    in_canary  = canary_count > 0
    canary_pct = round(canary_count / total, 4) if total > 0 else 0.0
    return in_canary, in_shadow, canary_pct


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

def run_lifecycle_checks(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    spans:      list[dict],
) -> LifecycleResult:
    """Run all agent lifecycle and version control checks.

    Args:
        db:         GovernanceDB instance.
        trace_id:   OTel trace ID.
        run_id:     Governance run ID.
        agent_role: Role of the agent being evaluated.
        spans:      List of span dicts as returned by db.get_spans_for_trace().

    Returns:
        LifecycleResult dataclass with all lifecycle check results.
    """
    # ── 1. Version extraction ─────────────────────────────────────────────
    model_versions_seen: list[str] = []
    try:
        model_versions_seen = _extract_model_versions(spans)
    except Exception as exc:
        log.warning("lifecycle_version_extraction_failed",
                    trace_id=trace_id, error=str(exc))

    # ── 2. Version pinning check ──────────────────────────────────────────
    is_pinned             = False
    pinned_version        = ""
    version_drift         = False

    try:
        pin_row = db.fetch_one(
            "SELECT pinned_version, is_pinned FROM otel.gov_version_pins FINAL "
            "WHERE agent_role = %(role)s",
            {"role": agent_role},
        )
        if pin_row:
            is_pinned      = bool(int(pin_row.get("is_pinned", 0) or 0))
            pinned_version = str(pin_row.get("pinned_version", "") or "")
            if is_pinned and pinned_version and model_versions_seen:
                # Drift if any observed version differs from the pin
                for v in model_versions_seen:
                    if v != pinned_version:
                        version_drift = True
                        break
    except Exception as exc:
        log.warning("lifecycle_version_pin_query_failed",
                    agent_role=agent_role, error=str(exc))

    # ── 3. Prompt drift rate (rolling 7-day) ─────────────────────────────
    prompt_drift_rate = 0.0
    try:
        drift_row = db.fetch_one(
            "SELECT countIf(is_drift=1) AS drift_count, count() AS total_count "
            "FROM otel.gov_prompt_drift "
            "WHERE agent_role = %(role)s AND checked_at >= now() - INTERVAL 7 DAY",
            {"role": agent_role},
        )
        if drift_row:
            total_count = int(drift_row.get("total_count", 0) or 0)
            drift_count = int(drift_row.get("drift_count", 0) or 0)
            if total_count > 0:
                prompt_drift_rate = drift_count / total_count
    except Exception as exc:
        log.warning("lifecycle_prompt_drift_rate_query_failed",
                    agent_role=agent_role, error=str(exc))

    # ── 4. Eval gate pass rate ────────────────────────────────────────────
    eval_gate_pass_rate = 1.0
    try:
        gate_row = db.fetch_one(
            "SELECT countIf(decision='block') AS blocks, count() AS total "
            "FROM otel.gov_policy_decisions "
            "WHERE run_id = %(run_id)s",
            {"run_id": run_id},
        )
        if gate_row:
            total  = int(gate_row.get("total",  0) or 0)
            blocks = int(gate_row.get("blocks", 0) or 0)
            if total > 0:
                eval_gate_pass_rate = 1.0 - (blocks / total)
    except Exception as exc:
        log.warning("lifecycle_eval_gate_query_failed",
                    run_id=run_id, error=str(exc))

    # ── 5. Change log check ───────────────────────────────────────────────
    change_detected = False
    try:
        if is_pinned and pinned_version and model_versions_seen:
            # Determine the single current version to compare (pick first seen)
            current_version = model_versions_seen[0]
            if current_version != pinned_version:
                change_detected = True
                try:
                    db.execute(
                        "INSERT INTO otel.gov_change_log "
                        "(agent_role, change_type, version_from, version_to, "
                        "changed_by, status) VALUES",
                        [(agent_role, "model", pinned_version, current_version,
                          "auto_detected", "applied")],
                    )
                    log.info(
                        "lifecycle_change_log_written",
                        agent_role=agent_role,
                        version_from=pinned_version,
                        version_to=current_version,
                    )
                except Exception as exc:
                    log.warning("lifecycle_change_log_insert_failed",
                                agent_role=agent_role, error=str(exc))
    except Exception as exc:
        log.warning("lifecycle_change_log_check_failed",
                    agent_role=agent_role, error=str(exc))

    # ── 6. Canary / shadow mode detection ────────────────────────────────
    in_canary  = False
    in_shadow  = False
    canary_pct = 0.0
    try:
        in_canary, in_shadow, canary_pct = _detect_deployment_modes(spans)
    except Exception as exc:
        log.warning("lifecycle_deployment_mode_detection_failed",
                    trace_id=trace_id, error=str(exc))

    # ── Persist metric snapshots ──────────────────────────────────────────
    version_pinning_compliance = (
        1.0 if (is_pinned and not version_drift) else 0.0
    )

    try:
        db.save_gov_metric(
            trace_id, "", run_id,
            "version_pinning_compliance",
            version_pinning_compliance,
            json.dumps({
                "is_pinned":     is_pinned,
                "pinned_version": pinned_version,
                "versions_seen": model_versions_seen,
                "drift":         version_drift,
            }),
        )
    except Exception as exc:
        log.warning("lifecycle_metric_save_failed",
                    metric="version_pinning_compliance", error=str(exc))

    try:
        db.save_gov_metric(
            trace_id, "", run_id,
            "prompt_drift_rate",
            prompt_drift_rate,
            "",
        )
    except Exception as exc:
        log.warning("lifecycle_metric_save_failed",
                    metric="prompt_drift_rate", error=str(exc))

    try:
        db.save_gov_metric(
            trace_id, "", run_id,
            "eval_gate_pass_rate",
            eval_gate_pass_rate,
            "",
        )
    except Exception as exc:
        log.warning("lifecycle_metric_save_failed",
                    metric="eval_gate_pass_rate", error=str(exc))

    result = LifecycleResult(
        trace_id=trace_id,
        agent_role=agent_role,
        model_versions_seen=model_versions_seen,
        is_pinned=is_pinned,
        pinned_version=pinned_version,
        version_drift_detected=version_drift,
        prompt_drift_rate=round(prompt_drift_rate, 6),
        eval_gate_pass_rate=round(eval_gate_pass_rate, 6),
        in_canary=in_canary,
        in_shadow=in_shadow,
        canary_pct=canary_pct,
        change_detected=change_detected,
    )

    log.info(
        "lifecycle_checks_complete",
        trace_id=trace_id,
        agent_role=agent_role,
        model_versions_seen=model_versions_seen,
        is_pinned=is_pinned,
        pinned_version=pinned_version,
        version_drift_detected=version_drift,
        prompt_drift_rate=result.prompt_drift_rate,
        eval_gate_pass_rate=result.eval_gate_pass_rate,
        in_canary=in_canary,
        in_shadow=in_shadow,
        canary_pct=canary_pct,
        change_detected=change_detected,
    )

    return result
