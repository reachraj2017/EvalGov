"""Regulatory Compliance & Risk Management — Categories 8 + 13.

Manages:
  1. Risk register CRUD (gov_risk_register)
  2. Compliance scorecard computation per regulatory framework
     (GDPR, SOC2, HIPAA, EU_AI_ACT, ISO42001)
  3. EU AI Act risk classification
  4. Regulatory scope CRUD (gov_regulatory_scope)
  5. Model registry CRUD (gov_model_registry)

Key public API:
  get_risk_register(db)                                        → list[dict]
  save_risk_item(db, ...)                                      → str (risk_id)
  compute_compliance_scorecard(db, framework)                  → dict
  get_regulatory_scope(db, agent_role="")                      → list[dict]
  save_regulatory_scope(db, ...)                               → str (scope_id)
  get_model_registry(db)                                       → list[dict]
  save_model_registry(db, ...)                                 → str
"""

import json
import uuid

import structlog

log = structlog.get_logger(__name__)

# Supported frameworks
FRAMEWORKS = {"GDPR", "SOC2", "HIPAA", "EU_AI_ACT", "ISO42001"}


# ──────────────────────────────────────────────────────────────────────────────
# Risk Register
# ──────────────────────────────────────────────────────────────────────────────

def get_risk_register(db) -> list[dict]:
    """Return all active risk register entries ordered by risk_score descending.

    Returns:
        List of risk register row dicts.
    """
    try:
        return db.fetch_all(
            "SELECT risk_id, title, category, likelihood, impact, risk_score, "
            "owner, mitigation, status, last_reviewed "
            "FROM otel.gov_risk_register FINAL "
            "WHERE status != 'deleted' "
            "ORDER BY risk_score DESC"
        )
    except Exception as exc:
        log.warning("get_risk_register_failed", error=str(exc))
        return []


def save_risk_item(
    db,
    title:      str,
    category:   str,
    likelihood: float,
    impact:     float,
    owner:      str      = "",
    mitigation: str      = "",
    status:     str      = "open",
    risk_id:    str      = "",
) -> str:
    """Upsert a risk register entry.  risk_score = likelihood * impact.

    Args:
        db:         GovernanceDB instance.
        title:      Short title for the risk.
        category:   Risk category (e.g. 'operational', 'compliance', 'safety').
        likelihood: Probability score, typically 1–5.
        impact:     Impact severity score, typically 1–5.
        owner:      Responsible party string.
        mitigation: Mitigation description.
        status:     'open' | 'mitigated' | 'accepted' | 'deleted'.
        risk_id:    Existing risk_id for upsert; empty string → generate new.

    Returns:
        The risk_id (new or existing).
    """
    rid        = risk_id or str(uuid.uuid4())
    risk_score = float(likelihood) * float(impact)
    try:
        db.execute(
            "INSERT INTO otel.gov_risk_register "
            "(risk_id, title, category, likelihood, impact, risk_score, "
            "owner, mitigation, status) VALUES",
            [(rid, title, category, float(likelihood), float(impact),
              risk_score, owner, mitigation, status)],
        )
    except Exception as exc:
        log.warning("save_risk_item_failed", risk_id=rid, error=str(exc))
    return rid


# ──────────────────────────────────────────────────────────────────────────────
# Compliance Scorecard helpers
# ──────────────────────────────────────────────────────────────────────────────

def _score_metric_threshold(
    db,
    metric:        str,
    threshold:     float,
    direction:     str   = "gte",
    detail_fmt:    str   = "",
    partial_ratio: float = 0.5,
    partial_mult:  float = 2.0,
) -> tuple[str, str]:
    """Query the latest value of a gov_metric_snapshots metric and score it.

    Args:
        db:            GovernanceDB instance.
        metric:        Metric name to look up.
        threshold:     Pass threshold.
        direction:     'gte' (value >= threshold → pass) or 'lte' (value <= threshold → pass).
        detail_fmt:    Template for evidence string; receives the actual value.
        partial_ratio: For 'gte': partial band lower bound = threshold * partial_ratio.
        partial_mult:  For 'lte': partial band upper bound = threshold * partial_mult.

    Returns:
        (status, evidence) where status is 'pass' | 'partial' | 'fail'.
    """
    try:
        row = db.fetch_one(
            "SELECT avg(value) AS avg_val FROM otel.gov_metric_snapshots "
            "WHERE metric = %(metric)s AND ts >= now() - INTERVAL 7 DAY",
            {"metric": metric},
        )
        if row and row.get("avg_val") is not None:
            val = float(row["avg_val"])
            evidence = detail_fmt.format(val) if detail_fmt else f"{metric}={round(val, 4)}"
            if direction == "gte":
                if val >= threshold:
                    return "pass", evidence
                elif val >= threshold * partial_ratio:
                    return "partial", evidence
                else:
                    return "fail", evidence
            else:  # lte
                if val <= threshold:
                    return "pass", evidence
                elif val <= threshold * partial_mult:
                    return "partial", evidence
                else:
                    return "fail", evidence
        return "fail", f"no data for {metric}"
    except Exception as exc:
        log.warning("score_metric_threshold_failed", metric=metric, error=str(exc))
        return "fail", f"query error: {str(exc)[:80]}"


def _score_table_has_rows(db, table: str, where: str = "", params: dict = None) -> tuple[str, str]:
    """Return 'pass' if the table has at least one qualifying row, else 'fail'."""
    try:
        where_clause = f"WHERE {where}" if where else ""
        row = db.fetch_one(
            f"SELECT count() AS cnt FROM {table} {where_clause}",
            params or {},
        )
        cnt = int((row or {}).get("cnt", 0) or 0)
        if cnt > 0:
            return "pass", f"{cnt} row(s) in {table}"
        return "fail", f"0 rows in {table}"
    except Exception as exc:
        log.warning("score_table_has_rows_failed", table=table, error=str(exc))
        return "fail", f"query error: {str(exc)[:80]}"


def _score_audit_completeness(
    db,
    pass_threshold:    float = 0.95,
    partial_threshold: float = 0.5,
) -> tuple[str, str]:
    """Score audit log completeness: ratio of traces with a governance_eval
    audit event to total distinct traces in gov_metric_snapshots (last 7 days).

    Args:
        db:                GovernanceDB instance.
        pass_threshold:    Coverage ratio required for 'pass'.
        partial_threshold: Coverage ratio required for 'partial'.
    """
    try:
        total_row = db.fetch_one(
            "SELECT count(DISTINCT trace_id) AS total "
            "FROM otel.gov_metric_snapshots "
            "WHERE ts >= now() - INTERVAL 7 DAY"
        )
        audited_row = db.fetch_one(
            "SELECT count(DISTINCT trace_id) AS audited "
            "FROM otel.gov_audit_log "
            "WHERE event_type = 'governance_eval' "
            "  AND ts >= now() - INTERVAL 7 DAY"
        )
        total   = int((total_row   or {}).get("total",   0) or 0)
        audited = int((audited_row or {}).get("audited", 0) or 0)
        if total == 0:
            return "fail", "no traces found in last 7 days"
        coverage = audited / total
        evidence = f"audit_coverage={round(coverage * 100, 1)}% ({audited}/{total})"
        if coverage >= pass_threshold:
            return "pass", evidence
        elif coverage >= partial_threshold:
            return "partial", evidence
        return "fail", evidence
    except Exception as exc:
        log.warning("score_audit_completeness_failed", error=str(exc))
        return "fail", f"query error: {str(exc)[:80]}"


def _score_prompt_snapshot_coverage(
    db,
    pass_threshold: float = 0.95,
    partial_ratio:  float = 0.5,
    partial_mult:   float = 2.0,
) -> tuple[str, str]:
    """Score prompt snapshot coverage from gov_metric_snapshots (last 7 days).

    Args:
        db:             GovernanceDB instance.
        pass_threshold: Coverage ratio required for 'pass'.
        partial_ratio:  Passed through to _score_metric_threshold.
        partial_mult:   Passed through to _score_metric_threshold.
    """
    return _score_metric_threshold(
        db,
        metric="prompt_snapshot_coverage",
        threshold=pass_threshold,
        direction="gte",
        detail_fmt="prompt_snapshot_coverage={:.4f}",
        partial_ratio=partial_ratio,
        partial_mult=partial_mult,
    )


def _score_pii_detection(
    db,
    pii_pass:     float = 0.01,
    partial_ratio: float = 0.5,
    partial_mult:  float = 2.0,
) -> tuple[str, str]:
    """Score PII detection: pass if pii_leak_rate is below pii_pass.

    Args:
        db:            GovernanceDB instance.
        pii_pass:      Upper bound for 'pass' (lte direction).
        partial_ratio: Passed through to _score_metric_threshold.
        partial_mult:  Passed through to _score_metric_threshold.
    """
    return _score_metric_threshold(
        db,
        metric="pii_leak_rate",
        threshold=pii_pass,
        direction="lte",
        detail_fmt="pii_leak_rate={:.4f}",
        partial_ratio=partial_ratio,
        partial_mult=partial_mult,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Per-framework control definitions and scoring
# ──────────────────────────────────────────────────────────────────────────────

def _score_gdpr(
    db,
    partial_ratio: float = 0.5,
    partial_mult:  float = 2.0,
    pii_pass:      float = 0.01,
    audit_pass:    float = 0.95,
    audit_partial: float = 0.5,
    snap_pass:     float = 0.95,
    avail_pass:    float = 0.99,
) -> list[dict]:
    """Score GDPR controls against available governance data."""
    controls = []

    # Art 5 — Auditability
    status, evidence = _score_audit_completeness(
        db,
        pass_threshold=audit_pass,
        partial_threshold=audit_partial,
    )
    controls.append({
        "id": "Art5",
        "description": "Audit trail completeness (Art. 5 accountability)",
        "status": status,
        "evidence": evidence,
    })

    # Art 32 — Security (PII leak rate)
    status, evidence = _score_pii_detection(
        db,
        pii_pass=pii_pass,
        partial_ratio=partial_ratio,
        partial_mult=partial_mult,
    )
    controls.append({
        "id": "Art32",
        "description": "PII leak rate < 1% (Art. 32 security measures)",
        "status": status,
        "evidence": evidence,
    })

    # Art 5(1)(e) — Retention policy
    status, evidence = _score_table_has_rows(
        db, "otel.gov_data_retention_policies"
    )
    controls.append({
        "id": "Art5_1_e",
        "description": "Data retention policy configured (Art. 5(1)(e))",
        "status": status,
        "evidence": evidence,
    })

    # Art 17 — Erasure requests handled
    status, evidence = _score_table_has_rows(
        db, "otel.gov_data_erasure_requests",
        where="status = 'completed'"
    )
    controls.append({
        "id": "Art17",
        "description": "Data erasure requests handled (Art. 17 right to erasure)",
        "status": status,
        "evidence": evidence,
    })

    # Art 13/14 — Transparency (prompt snapshot coverage)
    status, evidence = _score_prompt_snapshot_coverage(
        db,
        pass_threshold=snap_pass,
        partial_ratio=partial_ratio,
        partial_mult=partial_mult,
    )
    controls.append({
        "id": "Art13_14",
        "description": "Prompt snapshot coverage > 95% (Art. 13/14 transparency)",
        "status": status,
        "evidence": evidence,
    })

    return controls


def _score_soc2(
    db,
    partial_ratio: float = 0.5,
    partial_mult:  float = 2.0,
    pii_pass:      float = 0.01,
    audit_pass:    float = 0.95,
    audit_partial: float = 0.5,
    snap_pass:     float = 0.95,
    avail_pass:    float = 0.99,
) -> list[dict]:
    """Score SOC 2 controls against available governance data."""
    controls = []

    # CC6 — Access Control (API key auth enabled)
    status, evidence = _score_table_has_rows(
        db, "otel.gov_agent_keys",
        where="enabled = 1 AND revoked_at = '1970-01-01 00:00:00'"
    )
    controls.append({
        "id": "CC6",
        "description": "Access control: API key auth enabled (CC6)",
        "status": status,
        "evidence": evidence,
    })

    # CC7 — Monitoring (anomaly detection baselines)
    status, evidence = _score_table_has_rows(db, "otel.gov_agent_baselines")
    controls.append({
        "id": "CC7",
        "description": "Monitoring: anomaly detection baselines active (CC7)",
        "status": status,
        "evidence": evidence,
    })

    # CC2 — Policy (at least one policy enabled)
    status, evidence = _score_table_has_rows(
        db, "otel.gov_policies",
        where="enabled = 1"
    )
    controls.append({
        "id": "CC2",
        "description": "Communication and information: policies configured (CC2)",
        "status": status,
        "evidence": evidence,
    })

    # A1 — Availability (availability metric >= avail_pass)
    status, evidence = _score_metric_threshold(
        db,
        metric="availability",
        threshold=avail_pass,
        direction="gte",
        detail_fmt="availability={:.4f}",
        partial_ratio=partial_ratio,
        partial_mult=partial_mult,
    )
    controls.append({
        "id": "A1",
        "description": "Availability SLO >= 99% (A1)",
        "status": status,
        "evidence": evidence,
    })

    # P1-P8 — Privacy (PII detection active)
    status, evidence = _score_pii_detection(
        db,
        pii_pass=pii_pass,
        partial_ratio=partial_ratio,
        partial_mult=partial_mult,
    )
    controls.append({
        "id": "P1_P8",
        "description": "Privacy: PII detection rate within threshold (P1–P8)",
        "status": status,
        "evidence": evidence,
    })

    return controls


def _score_hipaa(
    db,
    partial_ratio: float = 0.5,
    partial_mult:  float = 2.0,
    pii_pass:      float = 0.01,
    audit_pass:    float = 0.95,
    audit_partial: float = 0.5,
    snap_pass:     float = 0.95,
    avail_pass:    float = 0.99,
) -> list[dict]:
    """Score HIPAA controls against available governance data."""
    controls = []

    # §164.312 — Audit Controls
    status, evidence = _score_audit_completeness(
        db,
        pass_threshold=audit_pass,
        partial_threshold=audit_partial,
    )
    controls.append({
        "id": "Sec164_312",
        "description": "Audit controls: governance audit trail (§164.312(b))",
        "status": status,
        "evidence": evidence,
    })

    # §164.308 — Access Management (API keys)
    status, evidence = _score_table_has_rows(
        db, "otel.gov_agent_keys",
        where="enabled = 1 AND revoked_at = '1970-01-01 00:00:00'"
    )
    controls.append({
        "id": "Sec164_308",
        "description": "Access management: API key authentication (§164.308(a)(4))",
        "status": status,
        "evidence": evidence,
    })

    # §164.514 — PHI minimum / PII detection
    status, evidence = _score_pii_detection(
        db,
        pii_pass=pii_pass,
        partial_ratio=partial_ratio,
        partial_mult=partial_mult,
    )
    controls.append({
        "id": "Sec164_514",
        "description": "Minimum necessary PHI: PII detection rate < 1% (§164.514)",
        "status": status,
        "evidence": evidence,
    })

    return controls


def _score_eu_ai_act(
    db,
    partial_ratio: float = 0.5,
    partial_mult:  float = 2.0,
    pii_pass:      float = 0.01,
    audit_pass:    float = 0.95,
    audit_partial: float = 0.5,
    snap_pass:     float = 0.95,
    avail_pass:    float = 0.99,
) -> list[dict]:
    """Score EU AI Act controls against available governance data."""
    controls = []

    # Art 9 — Risk Management System
    status, evidence = _score_table_has_rows(db, "otel.gov_risk_register")
    controls.append({
        "id": "Art9",
        "description": "Risk management system: risk register populated (Art. 9)",
        "status": status,
        "evidence": evidence,
    })

    # Art 10 — Data Governance (PII coverage)
    status, evidence = _score_pii_detection(
        db,
        pii_pass=pii_pass,
        partial_ratio=partial_ratio,
        partial_mult=partial_mult,
    )
    controls.append({
        "id": "Art10",
        "description": "Data governance: PII detection active (Art. 10)",
        "status": status,
        "evidence": evidence,
    })

    # Art 12 — Logging and Record-keeping
    status, evidence = _score_audit_completeness(
        db,
        pass_threshold=audit_pass,
        partial_threshold=audit_partial,
    )
    controls.append({
        "id": "Art12",
        "description": "Logging and record-keeping: audit trail completeness (Art. 12)",
        "status": status,
        "evidence": evidence,
    })

    # Art 13 — Transparency (prompt snapshot coverage)
    status, evidence = _score_prompt_snapshot_coverage(
        db,
        pass_threshold=snap_pass,
        partial_ratio=partial_ratio,
        partial_mult=partial_mult,
    )
    controls.append({
        "id": "Art13",
        "description": "Transparency: prompt snapshot coverage > 95% (Art. 13)",
        "status": status,
        "evidence": evidence,
    })

    # Classification — query gov_regulatory_scope for EU_AI_ACT classification
    classification = "unknown"
    try:
        scope_row = db.fetch_one(
            "SELECT classification FROM otel.gov_regulatory_scope FINAL "
            "WHERE framework = 'EU_AI_ACT' AND enabled = 1 LIMIT 1"
        )
        if scope_row:
            classification = str(scope_row.get("classification", "unknown") or "unknown")
    except Exception as exc:
        log.warning("eu_ai_act_classification_query_failed", error=str(exc))

    class_status   = "pass" if classification not in ("", "unknown") else "fail"
    controls.append({
        "id": "Art9_Classification",
        "description": "AI system risk classification recorded (Art. 9)",
        "status": class_status,
        "evidence": f"classification={classification}",
    })

    return controls


def _score_iso42001(
    db,
    partial_ratio: float = 0.5,
    partial_mult:  float = 2.0,
    pii_pass:      float = 0.01,
    audit_pass:    float = 0.95,
    audit_partial: float = 0.5,
    snap_pass:     float = 0.95,
    avail_pass:    float = 0.99,
) -> list[dict]:
    """Score ISO 42001 controls against available governance data."""
    controls = []

    # 6.1 — Actions to address risks (risk register completeness)
    status, evidence = _score_table_has_rows(db, "otel.gov_risk_register")
    controls.append({
        "id": "ISO_6_1",
        "description": "Risk and opportunity management: risk register present (6.1)",
        "status": status,
        "evidence": evidence,
    })

    # 6.5 — AI system lifecycle (change log has entries)
    status, evidence = _score_table_has_rows(db, "otel.gov_change_log")
    controls.append({
        "id": "ISO_6_5",
        "description": "AI system lifecycle management: change log active (6.5)",
        "status": status,
        "evidence": evidence,
    })

    # 8.4 — AI system reliability metrics available
    status, evidence = _score_table_has_rows(
        db, "otel.gov_reliability_metrics"
    )
    controls.append({
        "id": "ISO_8_4",
        "description": "Reliability metrics collected (8.4)",
        "status": status,
        "evidence": evidence,
    })

    return controls


# ──────────────────────────────────────────────────────────────────────────────
# Scorecard computation (public)
# ──────────────────────────────────────────────────────────────────────────────

def compute_compliance_scorecard(
    db,
    framework:  str,
    thresholds: dict = None,
) -> dict:
    """Compute a compliance scorecard for the specified regulatory framework.

    Each control is scored 0 (fail), 0.5 (partial), or 1.0 (pass) based on
    available data in the governance tables.  The overall score_pct is the
    percentage of maximum achievable points.

    Args:
        db:         GovernanceDB instance.
        framework:  One of GDPR | SOC2 | HIPAA | EU_AI_ACT | ISO42001.
        thresholds: Optional dict of threshold overrides.  Recognised keys:
                    regulatory.pii_pass_threshold,
                    regulatory.audit_coverage_pass,
                    regulatory.audit_coverage_partial,
                    regulatory.prompt_snapshot_coverage,
                    regulatory.availability_slo_pass,
                    regulatory.compliance_partial_ratio,
                    regulatory.compliance_partial_multiplier.

    Returns:
        Dict with keys: framework, total_controls, passing, partial, failing,
        score_pct, controls (list of control dicts).
    """
    t = thresholds or {}
    _pii_pass      = float(t.get("regulatory.pii_pass_threshold",           0.01))
    _audit_pass    = float(t.get("regulatory.audit_coverage_pass",           0.95))
    _audit_partial = float(t.get("regulatory.audit_coverage_partial",        0.5))
    _snap_pass     = float(t.get("regulatory.prompt_snapshot_coverage",      0.95))
    _avail_pass    = float(t.get("regulatory.availability_slo_pass",         0.99))
    _partial_ratio = float(t.get("regulatory.compliance_partial_ratio",      0.5))
    _partial_mult  = float(t.get("regulatory.compliance_partial_multiplier", 2.0))

    fw = framework.upper().replace("-", "_")
    if fw not in FRAMEWORKS:
        log.warning("compute_compliance_scorecard_unknown_framework", framework=framework)
        return {
            "framework":      framework,
            "total_controls": 0,
            "passing":        0,
            "partial":        0,
            "failing":        0,
            "score_pct":      0.0,
            "controls":       [],
            "error":          f"Unknown framework: {framework}",
        }

    _thresh_kwargs = dict(
        partial_ratio=_partial_ratio,
        partial_mult=_partial_mult,
        pii_pass=_pii_pass,
        audit_pass=_audit_pass,
        audit_partial=_audit_partial,
        snap_pass=_snap_pass,
        avail_pass=_avail_pass,
    )

    scorers = {
        "GDPR":      _score_gdpr,
        "SOC2":      _score_soc2,
        "HIPAA":     _score_hipaa,
        "EU_AI_ACT": _score_eu_ai_act,
        "ISO42001":  _score_iso42001,
    }

    controls: list[dict] = []
    try:
        controls = scorers[fw](db, **_thresh_kwargs)
    except Exception as exc:
        log.warning("compute_compliance_scorecard_scorer_failed",
                    framework=fw, error=str(exc))

    passing  = sum(1 for c in controls if c.get("status") == "pass")
    partial  = sum(1 for c in controls if c.get("status") == "partial")
    failing  = sum(1 for c in controls if c.get("status") == "fail")
    total    = len(controls)
    points   = passing * 1.0 + partial * 0.5
    max_pts  = float(total) if total > 0 else 1.0
    score_pct = round(points / max_pts * 100.0, 1)

    scorecard = {
        "framework":      fw,
        "total_controls": total,
        "passing":        passing,
        "partial":        partial,
        "failing":        failing,
        "score_pct":      score_pct,
        "controls":       controls,
    }

    # Persist to gov_compliance_scorecard
    try:
        db.execute(
            "INSERT INTO otel.gov_compliance_scorecard "
            "(framework, total_controls, passing_controls, passing_count, partial_count, failing_count, "
            "score_pct, detail) VALUES",
            [(fw, total, passing, passing, partial, failing,
              score_pct, json.dumps(scorecard))],
        )
    except Exception as exc:
        log.warning("compliance_scorecard_save_failed",
                    framework=fw, error=str(exc))

    log.info(
        "compliance_scorecard_computed",
        framework=fw,
        total_controls=total,
        passing=passing,
        partial=partial,
        failing=failing,
        score_pct=score_pct,
    )

    return scorecard


# ──────────────────────────────────────────────────────────────────────────────
# Regulatory Scope
# ──────────────────────────────────────────────────────────────────────────────

def get_regulatory_scope(db, agent_role: str = "") -> list[dict]:
    """Return enabled regulatory scope entries, optionally filtered by agent_role.

    Args:
        db:         GovernanceDB instance.
        agent_role: If non-empty, filter results to this agent role.

    Returns:
        List of regulatory scope row dicts.
    """
    try:
        where_extra = ""
        params: dict = {}
        if agent_role:
            where_extra = " AND agent_role = %(role)s"
            params["role"] = agent_role
        return db.fetch_all(
            "SELECT scope_id, agent_role, framework, enabled, sector, "
            "classification, notes "
            "FROM otel.gov_regulatory_scope FINAL "
            f"WHERE enabled = 1{where_extra}",
            params,
        )
    except Exception as exc:
        log.warning("get_regulatory_scope_failed",
                    agent_role=agent_role, error=str(exc))
        return []


def save_regulatory_scope(
    db,
    agent_role:     str,
    framework:      str,
    sector:         str  = "",
    classification: str  = "",
    notes:          str  = "",
    scope_id:       str  = "",
) -> str:
    """Upsert a regulatory scope entry.

    Args:
        db:             GovernanceDB instance.
        agent_role:     Agent role this scope applies to.
        framework:      Regulatory framework (e.g. 'GDPR', 'EU_AI_ACT').
        sector:         Industry sector (e.g. 'healthcare', 'finance').
        classification: EU AI Act or other risk classification label.
        notes:          Free-text notes.
        scope_id:       Existing scope_id for upsert; empty → generate new.

    Returns:
        The scope_id.
    """
    sid = scope_id or str(uuid.uuid4())
    try:
        db.execute(
            "INSERT INTO otel.gov_regulatory_scope "
            "(scope_id, agent_role, framework, enabled, sector, "
            "classification, notes) VALUES",
            [(sid, agent_role, framework.upper(), 1,
              sector, classification, notes)],
        )
    except Exception as exc:
        log.warning("save_regulatory_scope_failed",
                    scope_id=sid, agent_role=agent_role, error=str(exc))
    return sid


# ──────────────────────────────────────────────────────────────────────────────
# Model Registry
# ──────────────────────────────────────────────────────────────────────────────

def get_model_registry(db) -> list[dict]:
    """Return all model registry entries.

    Returns:
        List of model registry row dicts.
    """
    try:
        return db.fetch_all(
            "SELECT model_id, model_name, model_version, provider, license_type, "
            "commercial_ok, dpa_signed, baa_signed, sectors_allowed, notes, "
            "created_at "
            "FROM otel.gov_model_registry FINAL "
            "ORDER BY model_name ASC, model_version ASC"
        )
    except Exception as exc:
        log.warning("get_model_registry_failed", error=str(exc))
        return []


def save_model_registry(
    db,
    model_name:      str,
    model_version:   str,
    provider:        str      = "",
    license_type:    str      = "",
    commercial_ok:   int      = 1,
    dpa_signed:      int      = 0,
    baa_signed:      int      = 0,
    sectors_allowed: list     = None,
    notes:           str      = "",
    model_id:        str      = "",
) -> str:
    """Upsert a model registry entry.

    Args:
        db:              GovernanceDB instance.
        model_name:      Model name (e.g. 'claude-3-5-sonnet').
        model_version:   Version string (e.g. '20241022').
        provider:        Provider name (e.g. 'Anthropic').
        license_type:    License type string (e.g. 'commercial', 'apache-2.0').
        commercial_ok:   1 if approved for commercial use, 0 otherwise.
        dpa_signed:      1 if a Data Processing Agreement has been signed.
        baa_signed:      1 if a Business Associate Agreement has been signed.
        sectors_allowed: List of allowed sector strings.
        notes:           Free-text notes.
        model_id:        Existing model_id for upsert; empty → generate new.

    Returns:
        The model_id.
    """
    mid             = model_id or str(uuid.uuid4())
    sectors_allowed = sectors_allowed or []
    try:
        db.execute(
            "INSERT INTO otel.gov_model_registry "
            "(model_id, model_name, model_version, provider, license_type, "
            "commercial_ok, dpa_signed, baa_signed, sectors_allowed, notes) VALUES",
            [(mid, model_name, model_version, provider, license_type,
              int(commercial_ok), int(dpa_signed), int(baa_signed),
              sectors_allowed, notes)],
        )
    except Exception as exc:
        log.warning("save_model_registry_failed",
                    model_id=mid, model_name=model_name, error=str(exc))
    return mid
