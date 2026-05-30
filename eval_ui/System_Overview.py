"""
Agentic AI Evaluation & Governance Platform — Home
"""
import os
import streamlit as st

st.set_page_config(
    page_title="Agentic AI Eval & Governance",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

try:
    from db import db
    DB_AVAILABLE = True
except Exception as _import_err:
    DB_AVAILABLE = False
    _import_err_msg = str(_import_err)

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.title("Agentic AI Evaluation & Governance Platform")
st.markdown(
    "End-to-end visibility into multi-agent AI systems — from evaluation and "
    "observability to safety, compliance, and operational governance."
)
st.divider()

# ---------------------------------------------------------------------------
# System Status + Look-back window
# ---------------------------------------------------------------------------
st.subheader("System Status")
col_ch, col_win, col_spacer = st.columns([1, 2, 2])
with col_ch:
    if not DB_AVAILABLE:
        st.error(f"ClickHouse: UNAVAILABLE\n\n`{_import_err_msg}`")
    elif db.ping():
        st.success("ClickHouse: Connected")
    else:
        st.error("ClickHouse: Unreachable — check CLICKHOUSE_HOST / port settings")
with col_win:
    hours = st.select_slider(
        "Look-back window",
        options=[1, 6, 12, 24, 48, 168, 720],
        value=24,
        format_func=lambda x: {1: "1h", 6: "6h", 12: "12h", 24: "24h",
                                48: "48h", 168: "7d", 720: "30d"}.get(x, f"{x}h"),
    )

st.divider()

# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------
def _count(sql):
    try:
        rows = db._execute(sql)
        return int(rows[0]["cnt"]) if rows else 0
    except Exception:
        return 0

def _avg(sql):
    try:
        rows = db._execute(sql)
        v = rows[0].get("val") if rows else None
        return round(float(v), 3) if v is not None else None
    except Exception:
        return None

def _val(sql, key="val"):
    try:
        rows = db._execute(sql)
        v = rows[0].get(key) if rows else None
        return v
    except Exception:
        return None

# ---------------------------------------------------------------------------
# Pillar 1 — Eval Testing
# ---------------------------------------------------------------------------
st.subheader("🧪 Eval Testing")
st.caption("Runs, benchmarks, scores, and regression tracking for your AI agents.")

if DB_AVAILABLE:
    et_runs       = _count(f"SELECT count() AS cnt FROM otel.eval_runs WHERE created_at >= now() - INTERVAL {hours} HOUR")
    et_benchmarks = _count("SELECT count() AS cnt FROM otel.benchmarks WHERE active = 1")
    et_scores     = _count(f"SELECT count() AS cnt FROM otel.eval_scores WHERE evaluated_at >= now() - INTERVAL {hours} HOUR")
    et_avg        = _avg(f"SELECT avg(score) AS val FROM otel.eval_scores WHERE evaluated_at >= now() - INTERVAL {hours} HOUR")
    et_baseline   = _val("SELECT name AS val FROM otel.eval_runs WHERE is_baseline = 1 ORDER BY created_at DESC LIMIT 1", key="val")

    ec1, ec2, ec3, ec4, ec5 = st.columns(5)
    ec1.metric("Runs (window)",     et_runs,       help=f"Evaluation runs created in the last {hours}h")
    ec2.metric("Active Benchmarks", et_benchmarks, help="Benchmark test cases currently marked active (all time)")
    ec3.metric("Scores (window)",   et_scores,     help=f"Individual metric scores recorded in the last {hours}h")
    ec4.metric("Avg Score (window)", et_avg if et_avg is not None else "N/A",
               help=f"Average score across all metrics in the selected window")
    ec5.metric("Baseline Run",      et_baseline or "None set",
               help="Current baseline run used for regression comparisons")
else:
    st.warning("Database unavailable — metrics cannot be loaded.")

st.divider()

# ---------------------------------------------------------------------------
# Pillar 2 — Eval Measurements
# ---------------------------------------------------------------------------
st.subheader("📏 Eval Measurements")
st.caption("Live observability across conversations, prompt analysis, traces, and metric trends.")

if DB_AVAILABLE:
    em_traces  = _count(f"SELECT count() AS cnt FROM otel.otel_traces WHERE SpanName = 'agent.task' AND Timestamp >= now() - INTERVAL {hours} HOUR")
    em_prompts = _count(f"SELECT count() AS cnt FROM otel.prompt_evals WHERE created_at >= now() - INTERVAL {hours} HOUR")
    em_convs   = _count(
        f"SELECT countDistinct(SpanAttributes['conversation.id']) AS cnt "
        f"FROM otel.otel_traces "
        f"WHERE SpanAttributes['conversation.id'] != '' AND Timestamp >= now() - INTERVAL {hours} HOUR"
    )
    em_thresh  = _count("SELECT count() AS cnt FROM otel.alert_thresholds FINAL WHERE enabled = 1")
    em_viols   = _count(f"""
        SELECT count() AS cnt
        FROM otel.eval_scores es
        JOIN otel.alert_thresholds thresh ON es.metric = thresh.metric
        WHERE thresh.enabled = 1
          AND es.evaluated_at >= now() - INTERVAL {hours} HOUR
          AND (
            (thresh.operator = 'lt'  AND es.score < thresh.threshold) OR
            (thresh.operator = 'lte' AND es.score <= thresh.threshold) OR
            (thresh.operator = 'gt'  AND es.score > thresh.threshold) OR
            (thresh.operator = 'gte' AND es.score >= thresh.threshold)
          )
    """)

    mc1, mc2, mc3, mc4, mc5 = st.columns(5)
    mc1.metric("Agent Task Traces",      em_traces,  help=f"agent.task spans in the last {hours}h")
    mc2.metric("Prompt Evaluations",     em_prompts, help=f"Prompts scored in the last {hours}h")
    mc3.metric("Distinct Conversations", em_convs,   help=f"Unique conversation IDs seen in the last {hours}h")
    mc4.metric("Active Thresholds",      em_thresh,  help="Alert thresholds currently enabled (all time)")
    mc5.metric("Violations (window)",    em_viols,   help=f"Threshold breaches in the selected window")
else:
    st.warning("Database unavailable — metrics cannot be loaded.")

st.divider()

# ---------------------------------------------------------------------------
# Pillar 3 — AI Governance
# ---------------------------------------------------------------------------
st.subheader("🛡 AI Governance")
st.caption("Safety, identity, behavior, budget, compliance, and incident management for agentic systems.")

if DB_AVAILABLE:
    gov_metric_snapshots = _count(f"SELECT count() AS cnt FROM otel.gov_metric_snapshots WHERE ts >= now() - INTERVAL {hours} HOUR")
    gov_policy_decisions = _count(f"SELECT count() AS cnt FROM otel.gov_policy_decisions WHERE ts >= now() - INTERVAL {hours} HOUR")
    gov_audit_log        = _count(f"SELECT count() AS cnt FROM otel.gov_audit_log WHERE ts >= now() - INTERVAL {hours} HOUR")
    gov_token_budgets    = _count("SELECT countIf(enabled = 1) AS cnt FROM otel.gov_token_budgets FINAL")
    gov_open_incidents   = _count("SELECT countIf(status = 'open') AS cnt FROM otel.gov_incidents")
    gov_avg_compliance   = _avg(
        f"SELECT avg(score_pct) AS val FROM otel.gov_compliance_scorecard "
        f"WHERE computed_at >= now() - INTERVAL {hours} HOUR"
    )

    gc1, gc2, gc3, gc4, gc5, gc6 = st.columns(6)
    gc1.metric("Metric Snapshots",    gov_metric_snapshots,
               help=f"Governance metric snapshots in the last {hours}h")
    gc2.metric("Policy Decisions",    gov_policy_decisions,
               help=f"Policy enforcement decisions in the last {hours}h")
    gc3.metric("Audit Log Entries",   gov_audit_log,
               help=f"Audit trail entries in the last {hours}h")
    gc4.metric("Active Budgets",      gov_token_budgets,
               help="Agent token/cost daily budgets currently enabled (all time)")
    gc5.metric("Open Incidents",      gov_open_incidents,
               help="Governance incidents currently in open status (all time)")
    gc6.metric("Avg Compliance (window)",
               f"{gov_avg_compliance:.2f}" if gov_avg_compliance is not None else "N/A",
               help=f"Average compliance scorecard score in the selected window")
else:
    st.warning("Database unavailable — metrics cannot be loaded.")

st.divider()

# ---------------------------------------------------------------------------
# Pillar 4 — Governance Enforcement
# ---------------------------------------------------------------------------
st.subheader("⚡ Governance Enforcement")
st.caption("Pre-execution gate decisions, content quality gates, and human-in-the-loop approvals.")

if DB_AVAILABLE:
    enf_phase2 = _val(
        "SELECT value AS val FROM otel.gov_threshold_config FINAL "
        "WHERE config_key = 'enforcement.phase2_enabled' LIMIT 1"
    )
    enf_qg = _val(
        "SELECT value AS val FROM otel.gov_threshold_config FINAL "
        "WHERE config_key = 'enforcement.quality_gates_enabled' LIMIT 1"
    )
    enf_hitl_pending   = _count("SELECT count() AS cnt FROM otel.gov_hitl_queue FINAL WHERE status = 'pending'")
    enf_gate_checks    = _count(
        f"SELECT count() AS cnt FROM otel.gov_audit_log "
        f"WHERE event_type = 'gate_check' AND ts >= now() - INTERVAL {hours} HOUR"
    )
    enf_gate_blocks    = _count(
        f"SELECT count() AS cnt FROM otel.gov_audit_log "
        f"WHERE event_type = 'gate_check' "
        f"  AND JSONExtractString(detail, 'decision') = 'block' "
        f"  AND ts >= now() - INTERVAL {hours} HOUR"
    )
    enf_qg_decisions   = _count(
        f"SELECT count() AS cnt FROM otel.gov_quality_gate_decisions "
        f"WHERE created_at >= now() - INTERVAL {hours} HOUR"
    )
    enf_cb_open        = _count(
        "SELECT countIf(state = 'open') AS cnt FROM otel.gov_circuit_breakers FINAL"
    )
    enf_cb_half        = _count(
        "SELECT countIf(state = 'half_open') AS cnt FROM otel.gov_circuit_breakers FINAL"
    )

    phase2_label = "🟢 ON" if enf_phase2 and float(enf_phase2) >= 1 else "🔴 OFF"
    qg_label     = "🟢 ON" if enf_qg     and float(enf_qg)     >= 1 else "🔴 OFF"

    ef1, ef2, ef3, ef4, ef5, ef6, ef7, ef8 = st.columns(8)
    ef1.metric("Pre-Exec Enforcement", phase2_label,
               help="Whether the pre-execution gate is actively blocking/pausing agent actions")
    ef2.metric("Quality Gates",        qg_label,
               help="Whether content quality gate checks are active")
    ef3.metric("Pending HITL",         enf_hitl_pending,
               help="Human approvals currently awaiting review (all time)")
    ef4.metric("Gate Checks (window)", enf_gate_checks,
               help=f"Pre-execution gate evaluations in the last {hours}h")
    ef5.metric("Gate Blocks (window)", enf_gate_blocks,
               help=f"Gate decisions computed as 'block' in the last {hours}h")
    ef6.metric("QG Decisions (window)", enf_qg_decisions,
               help=f"Content quality gate flag/hold/block decisions in the last {hours}h")
    ef7.metric("CBs Open",             enf_cb_open,
               help="Circuit breakers currently in OPEN state (hard block)")
    ef8.metric("CBs Half-Open",        enf_cb_half,
               help="Circuit breakers in HALF_OPEN probe mode (next action requires approval)")
else:
    st.warning("Database unavailable — metrics cannot be loaded.")

st.divider()

# ---------------------------------------------------------------------------
# Navigation guide
# ---------------------------------------------------------------------------
st.subheader("Navigation")

col_l, col_r = st.columns(2)

with col_l:
    st.markdown("**🧪 Eval Testing**")
    st.markdown(
        "| Tab | Description |\n"
        "|-----|-------------|\n"
        "| Runs | Create runs, set baselines, trigger offline evaluation |\n"
        "| Benchmarks & Review | Manage test cases, run agents, human review queue |\n"
        "| Scores | Aggregate scores, distributions, and trends by run |\n"
        "| Regression | Compare two runs or a run vs baseline to catch regressions |"
    )

    st.markdown("**📏 Eval Measurements**")
    st.markdown(
        "| Tab | Description |\n"
        "|-----|-------------|\n"
        "| Conversations | Browse multi-turn conversations, drill into turns and scores |\n"
        "| Prompt Analysis | Filter and inspect prompt evals, trace drill-down, history |\n"
        "| Traces | Browse OTel traces, inspect span trees, view per-trace scores |\n"
        "| Eval Metrics | Live dashboard of all 56+ metrics across 9 categories |\n"
        "| Thresholds | Configure alert thresholds and view violations feed |"
    )

with col_r:
    st.markdown("**🛡 AI Governance**")
    st.markdown(
        "| Tab | Description |\n"
        "|-----|-------------|\n"
        "| Overview | High-level health across all governance domains |\n"
        "| Safety | Injection, jailbreak, toxicity, and bias detection |\n"
        "| Identity & Access | Auth failures, agent registry, tool permissions, credential leaks |\n"
        "| Behavior | Scope violations, persona drift, OOD output monitoring |\n"
        "| Budget & Cost | Token spend, cost per agent, budget utilization |\n"
        "| Anomalies | Statistical anomaly detection across agent metrics |\n"
        "| Incidents | Incident log, MTTD/MTTC, resolution tracking |\n"
        "| Reliability | Uptime, error rates, SLA compliance |\n"
        "| Compliance | GDPR, HIPAA, SOC 2, EU AI Act, ISO 42001 scorecards |\n"
        "| Audit | Full audit trail, data lineage, erasure requests |\n"
        "| Model Risk | Model cards, bias assessments, performance drift |\n"
        "| Data Governance | PII detection, data classification, retention policies |\n"
        "| Explainability | Decision audit trails, prompt snapshots, reasoning logs |\n"
        "| Human Oversight | Manual review queue, escalations, override log |\n"
        "| Policy Engine | Policy rules, enforcement decisions, gate check audit log |\n"
        "| Thresholds | Configurable governance thresholds (CB, budget, enforcement flags) |"
    )

    st.markdown("**⚡ Governance Enforcement**")
    st.markdown(
        "| Tab | Description |\n"
        "|-----|-------------|\n"
        "| Agent Pre-Execution | Enable/disable gate enforcement, trust scores, burn rates, circuit breakers |\n"
        "| Content Quality | Configure quality gate rules per agent/metric, view flag/hold/block decisions |\n"
        "| HITL Approvals | Review and approve or reject pending human-in-the-loop requests with full context |"
    )

st.divider()

# ---------------------------------------------------------------------------
# Getting started
# ---------------------------------------------------------------------------
with st.expander("Getting started"):
    st.markdown(
        """
**Eval Testing**

1. **Create a run** — go to *Eval Testing → Runs* and fill in the Create New Run form.
2. **Attach benchmarks** — link benchmark test cases to the run, then execute against your agent endpoint.
3. **Set a baseline** — once you have a stable run, mark it as the baseline for regression comparisons.
4. **Catch regressions** — use the *Regression* tab to compare any two runs and spot metric degradation immediately.
5. **Human review** — use the *Benchmarks & Review* tab to confirm or override AI-judge scores.

---

**Eval Measurements**

6. **Browse traces** — use *Eval Measurements → Traces* to drill into individual agent executions and span trees.
7. **Analyse prompts** — use *Prompt Analysis* to filter by agent, model, or conversation and inspect per-prompt scores.
8. **Monitor metrics** — the *Eval Metrics* tab shows live values for all 56+ metrics across 9 categories.
9. **Set alert thresholds** — configure thresholds in the *Thresholds* tab; violations surface in the feed automatically.

---

**AI Governance**

10. **Review safety events** — check *AI Governance → Safety* for any injection, jailbreak, or toxicity detections.
11. **Monitor incidents** — the *Incidents* tab tracks open issues with MTTD and MTTC metrics.
12. **Check compliance** — run scorecards for GDPR, HIPAA, SOC 2, EU AI Act, and ISO 42001 in the *Compliance* tab.
13. **Configure thresholds** — governance thresholds (CB settings, budget limits, enforcement flags) are configurable from the *Thresholds* tab without code changes.

---

**Governance Enforcement**

14. **Configure quality gates** — go to *Governance Enforcement → Content Quality* to set per-agent, per-metric thresholds with flag / hold / block actions.
15. **Enable enforcement** — use the *Agent Pre-Execution* tab to enable the gate; monitor trust scores, burn rates, and circuit breaker states per agent.
16. **Review HITL queue** — pending human approvals (from holds, CB half-open probes, or high-risk actions) appear in the *HITL Approvals* tab with full query/prompt context.
"""
    )
