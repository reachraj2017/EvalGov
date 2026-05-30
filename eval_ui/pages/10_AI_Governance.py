"""
Page: Governance & Compliance — All 13 Categories
Tabs:
  Summary
  1 · Auditability
  2 · Identity & Access
  3 · Data & Privacy
  4 · Safety & Guardrails
  5 · Policy Engine
  6 · Reliability / SRE
  7 · Behavior
  8 · Compliance Fit
  9 · Budget & Cost
  10 · Explainability
  11 · Lifecycle
  12 · Incidents
  13 · Regulatory
"""

import json
import os
import sys

import pandas as pd
import requests

_here    = os.path.dirname(os.path.abspath(__file__))
_ui_root = os.path.abspath(os.path.join(_here, ".."))
if _ui_root not in sys.path:
    sys.path.insert(0, _ui_root)

import streamlit as st

JAEGER_BASE = os.getenv("PUBLIC_JAEGER_URL", "http://localhost:16686")
GOV_URL     = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")

try:
    from db import db
    DB_AVAILABLE = True
except Exception as _e:
    DB_AVAILABLE = False
    _db_err = str(_e)

st.set_page_config(page_title="Governance & Compliance", page_icon="🛡️", layout="wide")
st.title("🛡️ Governance & Compliance")
st.caption(
    "13-category agentic-AI governance framework · "
    "Auditability · Identity · Privacy · Safety · Policy · Reliability · "
    "Behavior · Compliance · Budget · Explainability · Lifecycle · Incidents · Regulatory"
)

if not DB_AVAILABLE:
    st.error(f"Database unavailable: {_db_err}")
    st.stop()

# ── Ensure Phase 2 governance tables exist (idempotent) ──────────────────────
_P2_DDL = [
    """
    CREATE TABLE IF NOT EXISTS otel.gov_token_budgets (
        agent_role        String,
        daily_token_limit UInt64   DEFAULT 0,
        enabled           UInt8    DEFAULT 1,
        cost_usd_limit    Float32  DEFAULT 0.0,
        updated_at        DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY agent_role
    """,
    """
    CREATE TABLE IF NOT EXISTS otel.gov_prompt_drift (
        template_id    String,
        snapshot_hash  String,
        is_baseline    UInt8    DEFAULT 0,
        drift_detected UInt8    DEFAULT 0,
        first_seen     DateTime DEFAULT now(),
        last_seen      DateTime DEFAULT now(),
        seen_count     UInt64   DEFAULT 1
    ) ENGINE = ReplacingMergeTree(last_seen)
    ORDER BY (template_id, snapshot_hash)
    """,
    """
    CREATE TABLE IF NOT EXISTS otel.gov_routing_decisions (
        decision_id           String  DEFAULT generateUUIDv4(),
        trace_id              String  DEFAULT '',
        run_id                String  DEFAULT '',
        agent_role            String  DEFAULT '',
        complexity_tier       LowCardinality(String),
        model                 String,
        input_chars           UInt32  DEFAULT 0,
        estimated_savings_pct Float32 DEFAULT 0.0,
        ts                    DateTime DEFAULT now()
    ) ENGINE = MergeTree()
    ORDER BY (trace_id, ts)
    SETTINGS index_granularity = 8192
    """,
    """
    CREATE TABLE IF NOT EXISTS otel.gov_model_routing_config (
        complexity_tier   LowCardinality(String),
        model             String,
        cost_per_1m_input Float32  DEFAULT 3.0,
        max_input_tokens  UInt32   DEFAULT 16000,
        enabled           UInt8    DEFAULT 1,
        updated_at        DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY complexity_tier
    """,
]
for _ddl in _P2_DDL:
    try:
        db._execute_no_result(_ddl)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _gov(path, method="get", json_body=None, params=None):
    """Call the governance service and return parsed JSON, or None on error."""
    try:
        r = getattr(requests, method)(
            f"{GOV_URL}{path}",
            json=json_body,
            params=params,
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        st.error(f"Governance service error: {e}")
        return None


def _sev_color(sev: str) -> str:
    return {
        "critical": "#ff4444", "p1": "#ff4444",
        "high":     "#ff9900", "p2": "#ff9900",
        "medium":   "#f39c12",
        "low":      "#27ae60",
    }.get(str(sev).lower(), "#888")


# ─────────────────────────────────────────────────────────────────────────────
# Global controls
# ─────────────────────────────────────────────────────────────────────────────
ctrl1, ctrl2 = st.columns([3, 1])
with ctrl1:
    hours = st.select_slider(
        "Look-back window",
        options=[1, 6, 12, 24, 48, 72, 168, 336, 504, 720],
        value=24,
        format_func=lambda x: (
            f"Last {x}h" if x < 24
            else f"Last {x // 24}d" if x % 24 == 0
            else f"Last {x}h"
        ),
    )
with ctrl2:
    if st.button("↻ Refresh", use_container_width=True):
        st.rerun()

# ─────────────────────────────────────────────────────────────────────────────
# Tabs
# ─────────────────────────────────────────────────────────────────────────────
tabs = st.tabs([
    "Summary",
    "1 · Auditability",
    "2 · Identity & Access",
    "3 · Data & Privacy",
    "4 · Safety & Guardrails",
    "5 · Policy Engine",
    "6 · Reliability / SRE",
    "7 · Behavior",
    "8 · Compliance Fit",
    "9 · Budget & Cost",
    "10 · Explainability",
    "11 · Lifecycle",
    "12 · Incidents",
    "13 · Regulatory",
    "⚙ Thresholds",
])
(
    tab_summary, tab_audit, tab_identity, tab_privacy,
    tab_safety, tab_policy, tab_reliability, tab_behavior,
    tab_compliance, tab_budget, tab_explain, tab_lifecycle,
    tab_incidents, tab_regulatory, tab_thresholds,
) = tabs


# =============================================================================
# TAB 0 — Summary
# =============================================================================
with tab_summary:
    st.subheader("Governance Overview")

    full = _gov("/governance/full-summary", params={"hours": hours})
    if full:
        s1, s2, s3, s4, s5, s6, s7 = st.columns(7)
        s1.metric("Traces Scanned",    int(full.get("traces_scanned",    0) or 0))
        s2.metric("PII Output Hits",   int(full.get("pii_output_hits",   0) or 0))
        s3.metric("Safety Events",     int(full.get("safety_events",     0) or 0))
        s4.metric("Identity Events",   int(full.get("identity_events",   0) or 0))
        s5.metric("Incidents (window)",int(full.get("open_incidents",    0) or 0))
        s6.metric("Incidents (all open)", int(full.get("open_incidents_all", 0) or 0))
        s7.metric("Scope Violations",  int(full.get("scope_violations",  0) or 0))
    else:
        # Fallback to ClickHouse-backed summary
        summary = db.get_gov_summary(hours=hours)
        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Traces Scanned",  int(summary.get("traces_scanned",  0) or 0))
        s2.metric("PII Output Hits", int(summary.get("pii_output_hits", 0) or 0))
        s3.metric("Gate Blocks",     int(summary.get("total_blocks",    0) or 0))
        s4.metric("Gate Warnings",   int(summary.get("total_warnings",  0) or 0))

    st.divider()

    pol = _gov("/governance/summary", params={"hours": hours})
    if pol:
        st.subheader("Policy Gate Summary")
        pc1, pc2, pc3 = st.columns(3)
        pc1.metric("Blocks",   int(pol.get("total_blocks",   0) or 0))
        pc2.metric("Warnings", int(pol.get("total_warnings", 0) or 0))
        pc3.metric("Passes",   int(pol.get("total_passes",   0) or 0))

    st.divider()
    st.subheader("Compliance Scorecard")
    if st.button("Run Compliance Scorecard", key="run_scorecard_summary"):
        scorecard = _gov("/regulatory/scorecard")
        if scorecard:
            # Response is {framework_name: {score_pct, passing, ...}, ...}
            if isinstance(scorecard, list):
                rows = scorecard
            elif isinstance(scorecard, dict):
                rows = [v for v in scorecard.values() if isinstance(v, dict) and "framework" in v]
            else:
                rows = []
            if rows:
                df_sc = pd.DataFrame(rows)[["framework", "score_pct", "passing", "partial", "failing", "total_controls"]]
                df_sc = df_sc.rename(columns={
                    "framework": "Framework", "score_pct": "Score %",
                    "passing": "Pass", "partial": "Partial",
                    "failing": "Fail", "total_controls": "Controls",
                })
                df_sc = df_sc.sort_values("Score %", ascending=False)

                def _color_score(val):
                    try:
                        v = float(val)
                        if v >= 80: return "background-color:#d4edda;color:#155724"
                        if v >= 50: return "background-color:#fff3cd;color:#856404"
                        return "background-color:#f8d7da;color:#721c24"
                    except Exception:
                        return ""

                st.dataframe(
                    df_sc.style.applymap(_color_score, subset=["Score %"]),
                    use_container_width=True, hide_index=True,
                )
                # Bar chart
                import plotly.express as px
                fig = px.bar(df_sc, x="Framework", y="Score %", color="Score %",
                             color_continuous_scale=["#d73027","#fee090","#1a9850"],
                             range_color=[0, 100], text="Score %",
                             title="Compliance Scorecard — All Frameworks")
                fig.update_traces(texttemplate="%{text:.1f}%", textposition="outside")
                fig.update_layout(yaxis_range=[0, 110], showlegend=False, height=350)
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No scorecard data returned.")

    # Trend charts
    st.divider()
    st.subheader("Governance Metric Trends")
    ch1, ch2 = st.columns(2)
    with ch1:
        st.markdown("**PII Leak Rate**")
        pii_trend = db.get_gov_metric_trend("pii_leak_rate", hours=hours)
        if pii_trend:
            df_pii = pd.DataFrame(pii_trend).rename(columns={"hour": "Time", "avg_value": "PII Leak Rate"})
            st.area_chart(df_pii.set_index("Time")[["PII Leak Rate"]], height=180)
        else:
            st.caption("No data yet.")
    with ch2:
        st.markdown("**Prompt Snapshot Coverage**")
        snap_trend = db.get_gov_metric_trend("prompt_snapshot_coverage", hours=hours)
        if snap_trend:
            df_snap = pd.DataFrame(snap_trend).rename(columns={"hour": "Time", "avg_value": "Coverage"})
            st.area_chart(df_snap.set_index("Time")[["Coverage"]], height=180)
        else:
            st.caption("No data yet.")


# =============================================================================
# TAB 1 — Auditability & Trace Integrity
# =============================================================================
with tab_audit:
    st.subheader("Auditability & Trace Integrity")
    st.caption("Immutable audit trail, trace lineage, and audit log coverage.")

    # Audit trail coverage metric
    try:
        cov_rows = db._execute("""
            SELECT
                count(DISTINCT trace_id) AS audit_traces,
                (SELECT count(DISTINCT trace_id) FROM otel.gov_metric_snapshots) AS total_traces
            FROM otel.gov_audit_log
        """)
        if cov_rows:
            audit_traces = int(cov_rows[0].get("audit_traces", 0) or 0)
            total_traces = int(cov_rows[0].get("total_traces", 0) or 0)
            coverage_pct = (audit_traces / total_traces * 100) if total_traces > 0 else 0.0
            ac1, ac2, ac3 = st.columns(3)
            ac1.metric("Audit Log Traces", audit_traces)
            ac2.metric("Total Traces Seen", total_traces)
            ac3.metric("Audit Coverage", f"{coverage_pct:.1f}%")
    except Exception:
        pass

    st.divider()

    # Compliance reports list
    st.subheader("Compliance Reports")
    reports = db.get_gov_compliance_reports(limit=20)
    if not reports:
        st.info("No compliance reports generated yet.")
    else:
        for r in reports:
            rid   = str(r.get("report_id", ""))
            rtype = str(r.get("report_type", ""))
            f_ts  = str(r.get("from_ts", ""))[:10]
            t_ts  = str(r.get("to_ts",   ""))[:10]
            gen   = str(r.get("generated_by", "") or "")
            ts    = str(r.get("created_at",   ""))[:19]
            with st.expander(
                f"Report `{rid[:16]}` · {rtype} · {f_ts} → {t_ts} · by {gen or '—'} · {ts}",
                expanded=False,
            ):
                if st.button("Load full report", key=f"aud_load_{rid}"):
                    rep = _gov(f"/compliance/reports/{rid}")
                    if rep:
                        st.json(rep)

    st.divider()

    # Trace lineage lookup
    st.subheader("Trace Lineage Lookup")
    st.caption("Enter a trace ID to see span-by-span lineage, agents involved, PII events, and policy decisions.")
    lineage_tid = st.text_input("Trace ID", key="aud_lineage_tid", placeholder="Enter trace ID…")
    if lineage_tid and st.button("Fetch Lineage", key="aud_fetch_lineage"):
        lin = _gov(f"/compliance/lineage/{lineage_tid}")
        if lin:
            lc1, lc2, lc3 = st.columns(3)
            lc1.metric("Spans", lin.get("span_count", 0))
            lc2.metric("Agents Involved", len(lin.get("agents_involved", [])))
            lc3.metric("PII Events", len(lin.get("pii_events", [])))

            if lin.get("agents_involved"):
                st.markdown("**Agents:** " + " · ".join(f"`{a}`" for a in lin["agents_involved"]))

            spans = lin.get("spans") or lin.get("span_lineage") or []
            if spans:
                st.subheader("Span-by-Span Lineage")
                for sp in spans:
                    st.markdown(
                        f"- `{sp.get('span_id','')[:12]}` · **{sp.get('operation_name','') or sp.get('name','')}** "
                        f"· agent `{sp.get('agent_role','') or sp.get('service','')}` "
                        f"· {str(sp.get('ts','') or sp.get('start_time',''))[:19]}"
                    )

            if lin.get("policy_decisions"):
                st.subheader("Policy Decisions")
                for d in lin["policy_decisions"]:
                    dec  = str(d.get("decision", ""))
                    icon = {"block": "⛔", "warn": "⚠️", "pass": "✅"}.get(dec, "•")
                    st.markdown(
                        f"{icon} `{d.get('metric')}` = `{d.get('value', 0):.4f}` "
                        f"(threshold `{d.get('threshold', 0):.4f}`) → **{dec.upper()}**"
                    )

            if lin.get("pii_events"):
                st.subheader("PII Events")
                for p in lin["pii_events"]:
                    st.markdown(f"🔴 `{p.get('metric')}` · {str(p.get('ts', ''))[:19]}")

            with st.expander("Full lineage JSON", expanded=False):
                st.json(lin)

    st.divider()

    # Recent audit log
    st.subheader("Recent Audit Log Entries")
    log_limit = st.slider("Show last N entries", 10, 500, 50, step=10, key="aud_limit")
    audit_log = db.get_gov_audit_log(hours=hours, limit=log_limit)
    if not audit_log:
        st.info("Audit log is empty.")
    else:
        _EVENT_ICONS = {
            "governance_eval": "🔍",
            "gate_check":      "🚦",
            "hitl_request":    "👤",
            "policy_override": "⚙️",
        }
        for entry in audit_log:
            event_type = str(entry.get("event_type", ""))
            trace_id   = str(entry.get("trace_id",   "") or "")
            ts         = str(entry.get("ts", ""))[:19]
            detail_raw = str(entry.get("detail",     "") or "")
            icon       = _EVENT_ICONS.get(event_type, "📋")
            with st.expander(
                f"{icon} **{event_type}** · trace `{trace_id[:20]}` · {ts}",
                expanded=False,
            ):
                st.markdown(f"**Event:** `{event_type}`")
                st.markdown(f"**Trace:** `{trace_id}`")
                st.markdown(f"**Time:** {ts}")
                if trace_id:
                    st.markdown(f"[Open in Jaeger]({JAEGER_BASE}/trace/{trace_id})")
                if detail_raw:
                    try:
                        st.json(json.loads(detail_raw))
                    except Exception:
                        st.code(detail_raw[:500], language=None)


# =============================================================================
# TAB 2 — Identity & Access
# =============================================================================
with tab_identity:
    st.subheader("Identity & Access Control")
    st.caption("Agent identity, supply chain integrity, tool whitelists, API keys, and webhooks.")

    # Identity summary
    id_sum = _gov("/identity/summary", params={"hours": hours})
    if id_sum:
        ic1, ic2, ic3, ic4 = st.columns(4)
        ic1.metric("Total Identity Events", int(id_sum.get("total_events", 0) or 0))
        ic2.metric("Auth Failures",         int(id_sum.get("auth_failures", 0) or 0))
        ic3.metric("Unregistered Agents",   int(id_sum.get("unregistered_agents", 0) or 0))
        ic4.metric("Tool Violations",       int(id_sum.get("tool_violations", 0) or 0))
    else:
        st.info("Identity summary not available — governance service may be offline.")

    st.divider()

    # Identity events table
    st.subheader("Identity Events")
    id_events = _gov("/identity/events", params={"hours": hours, "limit": 50})
    if id_events:
        rows = id_events if isinstance(id_events, list) else id_events.get("events", [])
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
        else:
            st.info("No identity events yet.")
    else:
        st.info("No identity events yet.")

    st.divider()

    # Supply Chain Registry
    st.subheader("Supply Chain Registry")
    _SC_TYPES = ["model", "tool", "dependency", "container", "plugin"]
    sc_data = _gov("/supply-chain")
    sc_rows = (sc_data if isinstance(sc_data, list) else sc_data.get("items", [])) if sc_data else []
    if not sc_rows:
        st.info("No supply chain entries registered.")
    else:
        for sc in sc_rows:
            aid    = str(sc.get("artifact_id",   ""))
            atype  = str(sc.get("artifact_type", "") or "model")
            aname  = str(sc.get("artifact_name", "") or "")
            ahash  = str(sc.get("expected_hash", "") or "")
            averif = int(sc.get("verified",       1) or 0)
            with st.expander(
                f"{'✅' if averif else '⚠️'} **{aname}** · {atype}",
                expanded=False,
            ):
                with st.form(f"edit_sc_{aid}"):
                    ec1, ec2 = st.columns(2)
                    new_sc_type  = ec1.selectbox(
                        "Type", _SC_TYPES,
                        index=_SC_TYPES.index(atype) if atype in _SC_TYPES else 0,
                        key=f"sc_et_{aid}",
                    )
                    new_sc_name  = ec2.text_input("Name", value=aname, key=f"sc_en_{aid}")
                    new_sc_hash  = st.text_input("Expected hash", value=ahash, key=f"sc_eh_{aid}")
                    new_sc_verif = st.toggle("Verified", value=bool(averif), key=f"sc_ev_{aid}")
                    if st.form_submit_button("Save changes"):
                        _gov(f"/supply-chain/{aid}", method="put", json_body={
                            "artifact_type": new_sc_type,
                            "artifact_name": new_sc_name,
                            "expected_hash": new_sc_hash,
                            "verified":      1 if new_sc_verif else 0,
                        })
                        st.success("Updated.")
                        st.rerun()
                if st.button("Delete entry", key=f"del_sc_{aid}", type="secondary"):
                    _gov(f"/supply-chain/{aid}", method="delete")
                    st.warning("Deleted.")
                    st.rerun()

    with st.expander("Register new artifact", expanded=False):
        with st.form("add_supply_chain"):
            sc_col1, sc_col2 = st.columns(2)
            sc_type = sc_col1.selectbox("Artifact type", _SC_TYPES, key="sc_type")
            sc_name = sc_col2.text_input(
                "Artifact name (must match span attribute exactly, e.g. gpt-4o-mini not openai/gpt-4o-mini)",
                key="sc_name",
            )
            sc_hash = st.text_input(
                "Expected hash (SHA-256, or version string if hash unavailable)", key="sc_hash"
            )
            sc_verified = st.toggle(
                "Mark as verified", value=True, key="sc_verified",
                help="Verified=ON → passes supply chain check silently. "
                     "Verified=OFF → flags every trace using this artifact.",
            )
            if st.form_submit_button("Register"):
                if sc_name:
                    result = _gov("/supply-chain", method="post", json_body={
                        "artifact_type": sc_type,
                        "artifact_name": sc_name,
                        "expected_hash": sc_hash,
                        "verified":      1 if sc_verified else 0,
                    })
                    if result:
                        st.success(f"Artifact registered ({'verified' if sc_verified else 'UNVERIFIED — will trigger identity events'}).")
                        st.rerun()
                else:
                    st.warning("Artifact name is required.")

    st.divider()

    # Tool Whitelist
    st.subheader("Tool Whitelist")
    tw_data = _gov("/tool-whitelist")
    tw_rows = (tw_data if isinstance(tw_data, list) else tw_data.get("items", [])) if tw_data else []
    if not tw_rows:
        st.info("No tool whitelist entries yet.")
    else:
        for tw in tw_rows:
            eid      = str(tw.get("entry_id",   ""))
            tw_role_ = str(tw.get("agent_role", "") or "")
            tw_tool_ = str(tw.get("tool_name",  "") or "")
            allowed  = int(tw.get("allowed",     1) or 0)
            with st.expander(
                f"{'✅' if allowed else '🚫'} `{tw_role_}` / `{tw_tool_}`",
                expanded=False,
            ):
                with st.form(f"edit_tw_{eid}"):
                    tc1, tc2 = st.columns(2)
                    new_tw_role    = tc1.text_input("Agent role", value=tw_role_, key=f"tw_er_{eid}")
                    new_tw_tool    = tc2.text_input("Tool name",  value=tw_tool_, key=f"tw_et_{eid}")
                    new_tw_allowed = st.toggle("Allowed", value=bool(allowed), key=f"tw_ea_{eid}")
                    if st.form_submit_button("Save changes"):
                        _gov(f"/tool-whitelist/{eid}", method="put", json_body={
                            "agent_role": new_tw_role,
                            "tool_name":  new_tw_tool,
                            "allowed":    1 if new_tw_allowed else 0,
                        })
                        st.success("Updated.")
                        st.rerun()
                if st.button("Delete entry", key=f"del_tw_{eid}", type="secondary"):
                    _gov(f"/tool-whitelist/{eid}", method="delete")
                    st.warning("Deleted.")
                    st.rerun()

    with st.expander("Add whitelisted tool", expanded=False):
        with st.form("add_tool_whitelist"):
            tw_role = st.text_input("Agent role", key="tw_role")
            tw_tool = st.text_input("Tool name",  key="tw_tool")
            if st.form_submit_button("Add"):
                if tw_role and tw_tool:
                    result = _gov("/tool-whitelist", method="post", json_body={
                        "agent_role": tw_role, "tool_name": tw_tool,
                    })
                    if result:
                        st.success("Tool added to whitelist.")
                        st.rerun()
                else:
                    st.warning("Agent role and tool name are required.")

    st.divider()

    # Agent API Keys
    st.subheader("Agent API Keys")
    st.caption(
        "Keys are stored as SHA-256 hashes — the plaintext is shown once at creation. "
        "Agents include their key in the `X-Agent-Key` header."
    )
    with st.expander("Create new API key", expanded=False):
        new_role = st.text_input("Agent role", key="id_new_key_role")
        if st.button("Generate key", key="id_gen_key") and new_role:
            result = _gov("/agent-keys", method="post", json_body={"agent_role": new_role})
            if result:
                st.success(f"Key created for `{new_role}`")
                st.code(result.get("key", ""), language=None)
                st.warning(result.get("warning", "Save this key — it won't be shown again."))

    keys = db.get_gov_agent_keys()
    if not keys:
        st.info("No API keys created yet.")
    else:
        for k in keys:
            kid     = str(k.get("key_id",    ""))
            role    = str(k.get("agent_role", ""))
            prefix  = str(k.get("key_prefix", ""))
            _ek = k.get("enabled", 1)
            enabled = int(_ek) if _ek is not None else 1
            created = str(k.get("created_at", ""))[:19]
            with st.expander(
                f"{'🔑' if enabled else '🚫'} `{role}` · prefix `{prefix}` · created {created}",
                expanded=False,
            ):
                kc1, kc2, kc3 = st.columns(3)
                kc1.markdown(f"**Role:** `{role}`")
                kc1.markdown(f"**Prefix:** `{prefix}`")
                kc2.markdown(f"**Enabled:** {'Yes' if enabled else 'No'}")
                if enabled:
                    if kc3.button("Disable", key=f"id_disable_{kid}"):
                        result = _gov(f"/agent-keys/{kid}", method="put",
                                      json_body={"enabled": 0})
                        if result is not None:
                            st.warning("Key disabled.")
                            st.rerun()
                else:
                    if kc3.button("Enable", key=f"id_enable_{kid}"):
                        result = _gov(f"/agent-keys/{kid}", method="put",
                                      json_body={"enabled": 1})
                        if result is not None:
                            st.success("Key enabled.")
                            st.rerun()
                if st.button("Revoke key", key=f"id_revoke_{kid}"):
                    result = _gov(f"/agent-keys/{kid}", method="delete")
                    if result is not None:
                        st.warning("Key revoked.")
                        st.rerun()

    st.divider()

    # Webhook Notifications
    st.subheader("Webhook Notifications")
    _EVENTS = ["pii_detected", "budget_exceeded", "drift_detected", "gate_block", "anomaly", "all"]
    with st.expander("Add webhook", expanded=False):
        wh_name   = st.text_input("Name", key="id_wh_name")
        wh_url    = st.text_input("URL",  key="id_wh_url")
        wh_events = st.multiselect("Events to subscribe", _EVENTS, key="id_wh_events",
                                   default=["pii_detected", "gate_block"])
        wh_secret = st.text_input("Secret (HMAC signing)", key="id_wh_secret", type="password")
        if st.button("Save webhook", key="id_save_wh") and wh_url:
            result = _gov("/webhooks", method="post", json_body={
                "name": wh_name, "url": wh_url, "events": wh_events,
                "enabled": 1, "secret": wh_secret,
            })
            if result:
                st.success("Webhook saved.")
                st.rerun()

    webhooks = db.get_gov_webhooks()
    if not webhooks:
        st.info("No webhooks configured.")
    else:
        for wh in webhooks:
            wid     = str(wh.get("webhook_id", ""))
            name    = str(wh.get("name",    "") or "")
            url     = str(wh.get("url",     "") or "")
            events  = list(wh.get("events", []) or [])
            _ewh = wh.get("enabled", 1)
            enabled = int(_ewh) if _ewh is not None else 1
            with st.expander(
                f"{'🟢' if enabled else '🔴'} **{name or url[:40]}** "
                f"· {', '.join(events[:3])}{'…' if len(events) > 3 else ''}",
                expanded=False,
            ):
                with st.form(f"edit_wh_{wid}"):
                    new_wh_name    = st.text_input("Name", value=name, key=f"wh_en_{wid}")
                    new_wh_url     = st.text_input("URL",  value=url,  key=f"wh_eu_{wid}")
                    new_wh_events  = st.multiselect(
                        "Events", _EVENTS, default=events, key=f"wh_ee_{wid}",
                    )
                    new_wh_enabled = st.toggle(
                        "Enabled", value=bool(enabled), key=f"wh_ev_{wid}",
                    )
                    if st.form_submit_button("Save changes"):
                        _gov(f"/webhooks/{wid}", method="put", json_body={
                            "name":    new_wh_name,
                            "url":     new_wh_url,
                            "events":  new_wh_events,
                            "enabled": 1 if new_wh_enabled else 0,
                            "secret":  "",
                        })
                        st.success("Updated.")
                        st.rerun()
                if st.button("Delete webhook", key=f"del_wh_{wid}", type="secondary"):
                    _gov(f"/webhooks/{wid}", method="delete")
                    st.warning("Webhook deleted.")
                    st.rerun()


# =============================================================================
# TAB 3 — Data & Privacy (PII)
# =============================================================================
with tab_privacy:
    st.subheader("Data & Privacy")
    st.caption(
        "PII detection across inputs and outputs. "
        "Detects: email, SSN, credit card, US phone, IP, AWS keys, generic API keys."
    )

    # Metric cards from gov_metric_snapshots
    try:
        priv_rows = db._execute(f"""
            SELECT
                avgIf(value, metric = 'pii_leak_rate')   AS pii_leak_rate,
                countIf(metric = 'pii_in_input' AND value > 0)  AS pii_input_count,
                countIf(metric = 'pii_in_output' AND value > 0) AS pii_output_count
            FROM otel.gov_metric_snapshots
            WHERE ts >= now() - INTERVAL {int(hours)} HOUR
        """)
        if priv_rows:
            pr = priv_rows[0]
            pm1, pm2, pm3 = st.columns(3)
            pm1.metric("Avg PII Leak Rate",   f"{float(pr.get('pii_leak_rate', 0) or 0):.4f}")
            pm2.metric("PII Input Count",     int(pr.get("pii_input_count",  0) or 0))
            pm3.metric("PII Output Count",    int(pr.get("pii_output_count", 0) or 0))
    except Exception:
        pass

    st.divider()

    # PII events by type and span
    st.subheader("PII Events Breakdown")
    pii_spans = db.get_gov_pii_breakdown(hours=hours)
    if not pii_spans:
        st.info("No PII detected in outputs during this window.")
    else:
        # Aggregate PII type counts
        type_counts: dict = {}
        for row in pii_spans:
            try:
                parsed    = json.loads(str(row.get("detail", "") or ""))
                pii_types = parsed.get("pii_types", [])
            except Exception:
                pii_types = []
            for t in pii_types:
                type_counts[t] = type_counts.get(t, 0) + 1

        if type_counts:
            st.markdown("**PII type distribution (last window)**")
            df_types = pd.DataFrame(
                [{"PII Type": k, "Count": v} for k, v in sorted(type_counts.items(), key=lambda x: -x[1])]
            )
            st.dataframe(df_types, use_container_width=True)

        st.markdown(f"**{len(pii_spans)} flagged spans**")
        for row in pii_spans:
            trace_id = str(row.get("trace_id", "") or "")
            span_id  = str(row.get("span_id",  "") or "")
            ts       = str(row.get("ts", ""))[:19]
            try:
                pii_types = json.loads(str(row.get("detail", "") or "")).get("pii_types", [])
            except Exception:
                pii_types = []
            with st.expander(
                f"PII detected · trace `{trace_id[:20]}` · span `{span_id[:12]}` · {ts}",
                expanded=False,
            ):
                st.markdown(f"**Trace:** `{trace_id}`")
                st.markdown(f"**Span:** `{span_id}`")
                st.markdown(f"**PII types:** `{', '.join(pii_types) or 'unknown'}`")
                if trace_id:
                    st.markdown(f"[Open in Jaeger]({JAEGER_BASE}/trace/{trace_id})")

    st.divider()
    st.markdown("**Data Residency & Consent**")
    st.info(
        "Data residency enforcement and consent management are configuration-level items "
        "handled at the infrastructure layer (e.g., ClickHouse geo-partition, "
        "consent flags per user session). Not yet automated in this dashboard."
    )

    # PII trend charts
    st.divider()
    st.subheader("PII Trends")
    pc1, pc2 = st.columns(2)
    with pc1:
        st.markdown("**PII Leak Rate (outputs)**")
        pii_trend = db.get_gov_metric_trend("pii_leak_rate", hours=hours)
        if pii_trend:
            df_pt = pd.DataFrame(pii_trend).rename(columns={"hour": "Time", "avg_value": "PII Leak Rate"})
            st.area_chart(df_pt.set_index("Time")[["PII Leak Rate"]], height=200)
        else:
            st.caption("No data yet.")
    with pc2:
        st.markdown("**PII in Inputs**")
        input_trend = db.get_gov_metric_trend("pii_in_input", hours=hours)
        if input_trend:
            df_it = pd.DataFrame(input_trend).rename(columns={"hour": "Time", "avg_value": "Input PII Rate"})
            st.area_chart(df_it.set_index("Time")[["Input PII Rate"]], height=200)
        else:
            st.caption("No data yet.")


# =============================================================================
# TAB 4 — Safety & Guardrails
# =============================================================================
with tab_safety:
    st.subheader("Safety & Guardrails")
    st.caption("Prompt injection, jailbreak, toxic content, and bias detection.")

    # Summary from governance service
    saf_sum = _gov("/safety/summary", params={"hours": hours})
    if saf_sum:
        sf1, sf2, sf3, sf4 = st.columns(4)
        sf1.metric("Total Safety Events", int(saf_sum.get("total_events",     0) or 0))
        sf2.metric("Injections Detected", int(saf_sum.get("injection_count",  0) or 0))
        sf3.metric("Jailbreaks Detected", int(saf_sum.get("jailbreak_count",  0) or 0))
        sf4.metric("Toxic/Bias Events",   int(saf_sum.get("toxic_bias_count", 0) or 0))
    else:
        # Fallback via ClickHouse
        try:
            sf_rows = db.get_gov_safety_summary()
            if sf_rows is not None and not sf_rows.empty:
                st.dataframe(sf_rows, use_container_width=True)
        except Exception:
            st.info("Safety summary not available.")

    st.divider()

    # Safety events table
    st.subheader("Safety Events (last 50)")
    saf_events = _gov("/safety/events", params={"hours": hours, "limit": 50})
    if saf_events:
        se_rows = saf_events if isinstance(saf_events, list) else saf_events.get("events", [])
        if se_rows:
            _INJ_TYPES = {"injection", "jailbreak"}
            for se in se_rows:
                etype = str(se.get("event_type", "") or se.get("rule_type", ""))
                color = "#ff4444" if etype in _INJ_TYPES else "#ff9900"
                sev   = str(se.get("severity", "") or "")
                ts    = str(se.get("ts", "") or se.get("created_at", ""))[:19]
                with st.expander(
                    f"[{etype.upper()}] severity={sev} · {ts}",
                    expanded=False,
                ):
                    st.markdown(
                        f"<span style='color:{color};font-weight:bold'>{etype.upper()}</span>",
                        unsafe_allow_html=True,
                    )
                    for k, v in se.items():
                        if k not in ("event_type", "rule_type"):
                            st.markdown(f"**{k}:** `{v}`")
        else:
            st.info("No safety events yet.")
    else:
        st.info("No safety events yet.")

    st.divider()

    # Safety Rules management
    st.subheader("Safety Rules")
    rules = _gov("/safety/rules")
    if rules:
        rule_rows = rules if isinstance(rules, list) else rules.get("rules", [])
        if rule_rows:
            for rule in rule_rows:
                rid   = str(rule.get("rule_id", "") or rule.get("id", ""))
                rtype = str(rule.get("rule_type", ""))
                pat   = str(rule.get("pattern", ""))
                sev   = str(rule.get("severity", ""))
                desc  = str(rule.get("description", "") or "")
                color = "#ff4444" if rtype in ("injection", "jailbreak") else "#ff9900"
                with st.expander(
                    f"[{rtype.upper()}] {pat[:40]} · sev={sev}",
                    expanded=False,
                ):
                    st.markdown(
                        f"**Type:** <span style='color:{color}'>{rtype.upper()}</span>",
                        unsafe_allow_html=True,
                    )
                    st.markdown(f"**Pattern:** `{pat}`")
                    st.markdown(f"**Severity:** `{sev}`")
                    st.markdown(f"**Description:** {desc or '—'}")
                    if rid and st.button("Delete rule", key=f"del_rule_{rid}"):
                        _gov(f"/safety/rules/{rid}", method="delete")
                        st.warning("Rule deleted.")
                        st.rerun()
        else:
            st.info("No safety rules configured.")
    else:
        st.info("No safety rules data available.")

    with st.form("add_safety_rule"):
        st.markdown("**Add new safety rule**")
        r1, r2 = st.columns(2)
        nr_type    = r1.selectbox("Rule type", ["injection", "jailbreak", "toxic", "bias"], key="nr_type")
        nr_sev     = r2.selectbox("Severity", ["low", "medium", "high", "critical"], index=1, key="nr_sev")
        nr_pattern = st.text_input("Pattern (regex)", key="nr_pattern")
        nr_desc    = st.text_input("Description", key="nr_desc")
        if st.form_submit_button("Add Rule"):
            if nr_pattern:
                result = _gov("/safety/rules", method="post", json_body={
                    "rule_type": nr_type, "pattern": nr_pattern,
                    "severity": nr_sev, "description": nr_desc,
                })
                if result:
                    st.success("Safety rule added.")
                    st.rerun()
            else:
                st.warning("Pattern is required.")


# =============================================================================
# TAB 5 — Policy Engine
# =============================================================================
with tab_policy:
    GOVERNANCE_URL = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
    st.subheader("Policy Engine")
    st.caption(
        "Create and manage governance policy rules. "
        "Rules are evaluated against every trace's computed metrics. "
        "Changes take effect on the next governance evaluation."
    )

    policies = db.get_gov_policies()

    with st.expander("+ Add new policy rule", expanded=False):
        ef1, ef2, ef3 = st.columns(3)
        new_metric    = ef1.selectbox("Metric", [
            "pii_leak_rate", "pii_input_rate", "prompt_snapshot_coverage",
            "budget_utilization", "routing_savings_pct", "prompt_drift",
            "error_rate", "latency_p95", "availability",
        ], key="pe_new_metric")
        new_dir       = ef2.selectbox("Direction", ["gt", "gte", "lt", "lte"],
                                      key="pe_new_dir",
                                      format_func=lambda x: {
                                          "gt": "> (greater than)",
                                          "gte": ">= (greater or equal)",
                                          "lt": "< (less than)",
                                          "lte": "<= (less or equal)",
                                      }[x])
        new_decision  = ef3.selectbox("Decision", ["warn", "block"], key="pe_new_dec")
        ef4, ef5      = st.columns(2)
        new_threshold = ef4.number_input("Threshold", value=0.0, format="%.4f", key="pe_new_thr")
        new_desc      = ef5.text_input("Description", key="pe_new_desc")
        ef6, ef7      = st.columns(2)
        new_scope     = ef6.selectbox("Scope", ["global", "agent_role", "service"], key="pe_new_scope")
        new_sv        = ef7.text_input("Scope value (if scoped)", key="pe_new_sv")

        if st.button("Save rule", key="pe_save_new_policy"):
            try:
                import httpx as _hx
                resp = _hx.post(f"{GOVERNANCE_URL}/policies", json={
                    "metric": new_metric, "threshold": new_threshold,
                    "direction": new_dir, "decision": new_decision,
                    "scope": new_scope, "scope_value": new_sv,
                    "enabled": 1, "description": new_desc,
                }, timeout=5)
                resp.raise_for_status()
                st.success(f"Policy created: {resp.json().get('policy_id', '')[:16]}")
                st.rerun()
            except Exception as e:
                st.error(f"Failed: {e}")

    st.divider()

    if not policies:
        st.info("No policy rules yet. Add one above, or run the governance service to seed defaults.")
    else:
        _DIR_LABELS = {"gt": ">", "gte": ">=", "lt": "<", "lte": "<="}
        _DEC_COLORS = {"block": "#ff4444", "warn": "#ff9900"}
        _DEC_ICONS  = {"block": "⛔", "warn": "⚠️"}

        for p in policies:
            pid       = str(p.get("policy_id", ""))
            metric    = str(p.get("metric", ""))
            thr       = float(p.get("threshold", 0) or 0)
            direction = str(p.get("direction", "gt"))
            decision  = str(p.get("decision", "warn"))
            scope     = str(p.get("scope", "global"))
            sv        = str(p.get("scope_value", "") or "")
            _e = p.get("enabled", 1)
            enabled   = int(_e) if _e is not None else 1
            desc      = str(p.get("description", "") or "")
            icon      = _DEC_ICONS.get(decision, "•")
            color     = _DEC_COLORS.get(decision, "#888")
            scope_label = f" · `{scope}:{sv}`" if sv else (f" · `{scope}`" if scope != "global" else "")
            label = (
                f"{'' if enabled else '🚫 '}{icon} `{metric}` "
                f"{_DIR_LABELS.get(direction, direction)} `{thr}` → "
                f"**{decision.upper()}**{scope_label}"
            )

            with st.expander(label, expanded=False):
                pc1, pc2 = st.columns([3, 2])
                with pc1:
                    st.markdown(f"**ID:** `{pid[:24]}`")
                    st.markdown(f"**Description:** {desc or '—'}")
                    st.markdown(f"**Enabled:** {'Yes' if enabled else 'No'}")
                with pc2:
                    toggle_label = "Disable" if enabled else "Enable"
                    if st.button(toggle_label, key=f"pe_toggle_{pid}"):
                        try:
                            import httpx as _hx
                            _hx.put(f"{GOVERNANCE_URL}/policies/{pid}", json={
                                "metric": metric, "threshold": thr,
                                "direction": direction, "decision": decision,
                                "scope": scope, "scope_value": sv,
                                "enabled": 0 if enabled else 1,
                                "description": desc,
                            }, timeout=5).raise_for_status()
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

                    if st.button("Edit", key=f"pe_edit_btn_{pid}"):
                        st.session_state[f"pe_editing_{pid}"] = not st.session_state.get(f"pe_editing_{pid}", False)

                    if st.button("Backtest (7d)", key=f"pe_backtest_{pid}"):
                        try:
                            import httpx as _hx
                            r = _hx.post(f"{GOVERNANCE_URL}/policies/backtest", json={
                                "metric": metric, "threshold": thr,
                                "direction": direction, "decision": decision,
                                "hours": 168,
                            }, timeout=10)
                            r.raise_for_status()
                            bt = r.json()
                            st.info(
                                f"Backtest (7d): **{bt.get('would_trigger', 0)}** of "
                                f"**{bt.get('total_traces', 0)}** traces would trigger "
                                f"({bt.get('trigger_pct', 0):.1f}%)"
                            )
                        except Exception as e:
                            st.error(str(e))

                    if st.button("Delete", key=f"pe_del_{pid}"):
                        try:
                            import httpx as _hx
                            _hx.delete(f"{GOVERNANCE_URL}/policies/{pid}", timeout=5).raise_for_status()
                            st.warning("Rule disabled.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))

                # ── Inline edit form (toggled by Edit button) ──────────────
                if st.session_state.get(f"pe_editing_{pid}", False):
                    st.divider()
                    st.markdown("**Edit rule**")
                    _all_metrics = [
                        "pii_leak_rate", "pii_input_rate", "prompt_snapshot_coverage",
                        "budget_utilization", "routing_savings_pct", "prompt_drift",
                        "error_rate", "latency_p95", "availability",
                    ]
                    _mi = _all_metrics.index(metric) if metric in _all_metrics else 0
                    ef_c1, ef_c2, ef_c3 = st.columns(3)
                    e_metric    = ef_c1.selectbox("Metric", _all_metrics,
                                                  index=_mi, key=f"pe_em_{pid}")
                    e_dir       = ef_c2.selectbox("Direction", ["gt", "gte", "lt", "lte"],
                                                  index=["gt","gte","lt","lte"].index(direction),
                                                  format_func=lambda x: {"gt":">","gte":">=","lt":"<","lte":"<="}[x],
                                                  key=f"pe_ed_{pid}")
                    e_decision  = ef_c3.selectbox("Decision", ["warn", "block"],
                                                  index=["warn","block"].index(decision) if decision in ["warn","block"] else 0,
                                                  key=f"pe_edec_{pid}")
                    ef_c4, ef_c5 = st.columns(2)
                    e_threshold = ef_c4.number_input("Threshold", value=float(thr),
                                                     format="%.4f", key=f"pe_ethr_{pid}")
                    e_desc      = ef_c5.text_input("Description", value=desc, key=f"pe_edesc_{pid}")
                    ef_c6, ef_c7 = st.columns(2)
                    e_scope     = ef_c6.selectbox("Scope", ["global", "agent_role", "service"],
                                                  index=["global","agent_role","service"].index(scope) if scope in ["global","agent_role","service"] else 0,
                                                  key=f"pe_escope_{pid}")
                    e_sv        = ef_c7.text_input("Scope value", value=sv, key=f"pe_esv_{pid}")
                    save_c, cancel_c = st.columns(2)
                    if save_c.button("Save changes", key=f"pe_esave_{pid}"):
                        try:
                            import httpx as _hx
                            _hx.put(f"{GOVERNANCE_URL}/policies/{pid}", json={
                                "metric": e_metric, "threshold": e_threshold,
                                "direction": e_dir, "decision": e_decision,
                                "scope": e_scope, "scope_value": e_sv,
                                "enabled": enabled, "description": e_desc,
                            }, timeout=5).raise_for_status()
                            st.session_state.pop(f"pe_editing_{pid}", None)
                            st.success("Rule updated.")
                            st.rerun()
                        except Exception as e:
                            st.error(str(e))
                    if cancel_c.button("Cancel", key=f"pe_ecancel_{pid}"):
                        st.session_state.pop(f"pe_editing_{pid}", None)
                        st.rerun()

    st.divider()
    st.subheader("Policy Gate Decisions")
    dec_filter = st.selectbox("Filter by decision", ["All", "block", "warn", "pass"], key="pe_policy_filter")
    _df = dec_filter if dec_filter != "All" else ""
    decisions = db.get_gov_policy_decisions(hours=hours, decision_filter=_df, limit=200)
    if not decisions:
        st.info("No policy decisions in this window.")
    else:
        d_counts = {"block": 0, "warn": 0, "pass": 0}
        for d in decisions:
            k = str(d.get("decision", "")).lower()
            if k in d_counts:
                d_counts[k] += 1
        dc1, dc2, dc3, dc4 = st.columns(4)
        dc1.metric("Showing",  len(decisions))
        dc2.metric("Block",    d_counts["block"])
        dc3.metric("Warn",     d_counts["warn"])
        dc4.metric("Pass",     d_counts["pass"])

        _COLORS = {"block": "#ff4444", "warn": "#ff9900", "pass": "#27ae60"}
        _ICONS  = {"block": "⛔", "warn": "⚠️", "pass": "✅"}
        for d in decisions[:100]:
            decision  = str(d.get("decision", "")).lower()
            metric    = d.get("metric", "")
            value     = float(d.get("value",     0) or 0)
            threshold = float(d.get("threshold", 0) or 0)
            trace_id  = str(d.get("trace_id", "") or "")
            ts        = str(d.get("ts", ""))[:19]
            icon      = _ICONS.get(decision, "•")
            with st.expander(
                f"{icon} **{metric}** · {decision.upper()} · trace `{trace_id[:16]}` · {ts}",
                expanded=(decision == "block"),
            ):
                c1, c2 = st.columns(2)
                c1.markdown(f"**Metric:** `{metric}`")
                c1.markdown(
                    f"**Decision:** <span style='color:{_COLORS.get(decision, '#888')};font-weight:bold'>"
                    f"{decision.upper()}</span>",
                    unsafe_allow_html=True,
                )
                c2.markdown(f"**Value:** `{value:.4f}` | **Threshold:** `{threshold:.4f}`")
                if trace_id:
                    st.markdown(f"[Open in Jaeger]({JAEGER_BASE}/trace/{trace_id})")

    st.divider()
    st.subheader("HITL Queue")
    hitl_pending, hitl_approved, hitl_rejected = st.tabs(["Pending", "Approved", "Rejected"])
    _TIER_COLORS = {
        "critical": "#ff4444", "high": "#ff9900", "medium": "#f39c12", "low": "#27ae60",
    }

    with hitl_pending:
        pending = db.get_gov_hitl_queue(status="pending")
        if not pending:
            st.info("No pending HITL requests.")
        else:
            _qg_pending = [r for r in pending if str(r.get("action_type", "")) in ("quality_gate_hold", "quality_gate_block")]
            _hdr_col, _btn_col = st.columns([3, 2])
            _hdr_col.markdown(f"**{len(pending)} pending request(s)**")
            if _qg_pending and _btn_col.button(
                f"Dismiss All Quality Gate History ({len(_qg_pending)})",
                type="secondary",
                use_container_width=True,
                help="Marks all pending quality gate holds and blocks as expired. No CB impact.",
            ):
                try:
                    _expired = db.bulk_expire_quality_gate_hitl(reviewer="operator")
                    st.success(f"Dismissed {_expired} quality gate HITL entries.")
                    st.rerun()
                except Exception as _e:
                    st.error(f"Failed: {_e}")
            for req in pending:
                req_id      = str(req.get("request_id", ""))
                trace_id    = str(req.get("trace_id",   "") or "")
                risk_tier   = str(req.get("risk_tier",  "low"))
                action_type = str(req.get("action_type","") or "")
                payload     = str(req.get("payload",    "") or "")
                created_at  = str(req.get("created_at", ""))[:19]
                tier_color  = _TIER_COLORS.get(risk_tier.lower(), "#666")

                with st.expander(
                    f"[{risk_tier.upper()}] {action_type or 'unknown'} · {created_at}",
                    expanded=(risk_tier.lower() in ("high", "critical")),
                ):
                    h1, h2 = st.columns([3, 2])
                    with h1:
                        st.markdown(
                            f"**Risk tier:** <span style='color:{tier_color};font-weight:bold'>"
                            f"{risk_tier.upper()}</span>",
                            unsafe_allow_html=True,
                        )
                        st.markdown(f"**Trace:** `{trace_id}`")
                        if payload:
                            try:
                                st.json(json.loads(payload))
                            except Exception:
                                st.code(payload[:500], language=None)
                    with h2:
                        reviewer = st.text_input("Reviewer name", key=f"pe_rev_{req_id}")
                        notes    = st.text_area("Decision notes", key=f"pe_notes_{req_id}", height=80)
                        a_col, r_col = st.columns(2)
                        if a_col.button("Approve", key=f"pe_approve_{req_id}", use_container_width=True):
                            try:
                                db.update_gov_hitl_decision(req_id, "approved", reviewer, notes)
                                st.success("Approved.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")
                        if r_col.button("Reject", key=f"pe_reject_{req_id}", use_container_width=True):
                            try:
                                db.update_gov_hitl_decision(req_id, "rejected", reviewer, notes)
                                st.warning("Rejected.")
                                st.rerun()
                            except Exception as e:
                                st.error(f"Failed: {e}")

    with hitl_approved:
        approved = db.get_gov_hitl_queue(status="approved")
        if not approved:
            st.info("No approved requests.")
        else:
            _cols = ["request_id", "trace_id", "risk_tier", "action_type", "reviewer", "decided_at"]
            df_app = pd.DataFrame(approved)
            st.dataframe(df_app[[c for c in _cols if c in df_app.columns]], use_container_width=True)

    with hitl_rejected:
        rejected = db.get_gov_hitl_queue(status="rejected")
        if not rejected:
            st.info("No rejected requests.")
        else:
            _cols = ["request_id", "trace_id", "risk_tier", "action_type", "reviewer", "notes", "decided_at"]
            df_rej = pd.DataFrame(rejected)
            st.dataframe(df_rej[[c for c in _cols if c in df_rej.columns]], use_container_width=True)

    st.divider()
    st.subheader("Gate Checks Summary")
    gate_checks = db.get_gov_gate_checks(hours=hours, limit=100)
    if not gate_checks:
        st.info("No gate checks in this window.")
    else:
        df_gc = pd.DataFrame(gate_checks)
        st.dataframe(df_gc, use_container_width=True)


# =============================================================================
# TAB 6 — Reliability / SRE
# =============================================================================
with tab_reliability:
    st.subheader("Reliability / SRE")
    st.caption("Error rates, latency SLOs, availability, and budget consumption by agent role.")

    rel_sum = _gov("/reliability/summary", params={"hours": hours})
    # endpoint returns a list (one row per agent_role); aggregate for top-level metrics
    if rel_sum:
        rows = rel_sum if isinstance(rel_sum, list) else [rel_sum]
        def _avg(key):
            vals = [float(r.get(key, 0) or 0) for r in rows if isinstance(r, dict)]
            return sum(vals) / len(vals) if vals else 0.0
        rm1, rm2, rm3, rm4 = st.columns(4)
        rm1.metric("Avg Error Rate",       f"{_avg('avg_error_rate'):.4f}")
        rm2.metric("Avg p95 Latency (ms)", f"{_avg('avg_p95_ms'):.0f}")
        rm3.metric("Avg Availability",     f"{_avg('avg_availability'):.4f}")
        rm4.metric("Avg Budget Consumed",  f"{_avg('avg_budget_consumed'):.4f}")
    else:
        st.info("Reliability summary not available.")

    st.divider()

    st.subheader("Reliability Metrics (last 20 records)")
    rel_data = _gov("/reliability", params={"hours": hours, "limit": 20})
    if rel_data:
        rel_rows = rel_data if isinstance(rel_data, list) else rel_data.get("items", [])
        if rel_rows:
            df_rel = pd.DataFrame(rel_rows)
            # Color-code SLA breaches inline
            st.dataframe(df_rel, use_container_width=True)
        else:
            st.info("No reliability data yet.")
    else:
        st.info("No reliability data yet.")

    st.divider()

    # SLO Configuration
    st.subheader("SLO Configuration")
    slo_data = _gov("/slo")
    if slo_data:
        slo_rows = slo_data if isinstance(slo_data, list) else slo_data.get("items", [])
        if slo_rows:
            df_slo = pd.DataFrame(slo_rows)
            st.dataframe(df_slo, use_container_width=True)
        else:
            st.info("No SLOs configured yet.")
    else:
        st.info("No SLO data available.")

    with st.form("add_slo"):
        st.markdown("**Add / Update SLO**")
        sl1, sl2, sl3 = st.columns(3)
        slo_role  = sl1.text_input("Agent role", key="slo_role")
        slo_avail = sl2.number_input("Target availability", value=0.999, format="%.4f", key="slo_avail")
        slo_eb    = sl3.number_input("Error budget %", value=0.001, format="%.4f", key="slo_eb")
        sl4, sl5, sl6 = st.columns(3)
        slo_days  = sl4.number_input("SLO window (days)", value=30, step=1, key="slo_days")
        slo_p95   = sl5.number_input("p95 target (ms)", value=2000, step=100, key="slo_p95")
        slo_p99   = sl6.number_input("p99 target (ms)", value=5000, step=100, key="slo_p99")
        if st.form_submit_button("Save SLO"):
            if slo_role:
                result = _gov("/slo", method="post", json_body={
                    "agent_role": slo_role,
                    "target_availability": slo_avail,
                    "error_budget_pct": slo_eb,
                    "slo_window_days": int(slo_days),
                    "p95_target_ms": int(slo_p95),
                    "p99_target_ms": int(slo_p99),
                })
                if result:
                    st.success("SLO saved.")
                    st.rerun()
            else:
                st.warning("Agent role is required.")


# =============================================================================
# TAB 7 — Behavior & Consistency
# =============================================================================
with tab_behavior:
    st.subheader("Behavior & Consistency")
    st.caption("Scope violations, persona drift, and behavioral anomaly detection.")

    beh_sum = _gov("/behavior/summary", params={"hours": hours})
    if beh_sum:
        bm1, bm2, bm3 = st.columns(3)
        bm1.metric("Scope Violations",  int(beh_sum.get("scope_violations",  0) or 0))
        bm2.metric("Persona Drifts",    int(beh_sum.get("persona_drifts",    0) or 0))
        bm3.metric("Total Behavior Events", int(beh_sum.get("total_events",  0) or 0))
    else:
        st.info("Behavior summary not available.")

    st.divider()

    st.subheader("Scope Violations (last 50)")
    beh_events = _gov("/behavior/events", params={"hours": hours, "limit": 50})
    if beh_events:
        be_rows = beh_events if isinstance(beh_events, list) else beh_events.get("events", [])
        if be_rows:
            st.dataframe(pd.DataFrame(be_rows), use_container_width=True)
        else:
            st.info("No scope violations yet.")
    else:
        st.info("No scope violation data available.")

    st.divider()

    # Persona Configuration
    st.subheader("Persona Configuration")
    persona_data = _gov("/persona")
    if persona_data:
        p_rows = persona_data if isinstance(persona_data, list) else persona_data.get("items", [])
        if p_rows:
            st.dataframe(pd.DataFrame(p_rows), use_container_width=True)
        else:
            st.info("No persona configurations yet.")
    else:
        st.info("No persona data available.")

    with st.form("add_persona"):
        st.markdown("**Add persona**")
        pa1, pa2 = st.columns(2)
        per_role   = pa1.text_input("Agent role", key="per_role")
        per_topics = pa2.text_area("Authorized topics (comma-separated)", key="per_topics", height=80)
        per_desc   = st.text_area("Persona description", key="per_desc", height=80)
        if st.form_submit_button("Save Persona"):
            if per_role:
                topics = [t.strip() for t in per_topics.split(",") if t.strip()]
                result = _gov("/persona", method="post", json_body={
                    "agent_role": per_role,
                    "authorized_topics": topics,
                    "persona_description": per_desc,
                })
                if result:
                    st.success("Persona saved.")
                    st.rerun()
            else:
                st.warning("Agent role is required.")

    st.divider()

    # Anomaly Detection
    st.subheader("Behavioral Anomaly Detection")
    st.caption("Statistical deviation (mean ± stddev). Severity: medium ≥ 2σ · high ≥ 3σ · critical ≥ 4σ.")

    anomaly_summary = db.get_gov_anomaly_summary(hours=hours)
    total_anoms = int(anomaly_summary.get("total",          0) or 0)
    crit_anoms  = int(anomaly_summary.get("critical_count", 0) or 0)
    high_anoms  = int(anomaly_summary.get("high_count",     0) or 0)
    med_anoms   = int(anomaly_summary.get("medium_count",   0) or 0)

    am1, am2, am3, am4 = st.columns(4)
    am1.metric("Total anomalies", total_anoms)
    am2.metric("Critical (>=4s)", crit_anoms)
    am3.metric("High (>=3s)",     high_anoms)
    am4.metric("Medium (>=2s)",   med_anoms)

    if crit_anoms > 0:
        st.error(f"⛔ {crit_anoms} critical anomaly event(s) in the last {hours}h")
    elif high_anoms > 0:
        st.warning(f"⚠️ {high_anoms} high-severity anomaly event(s) in the last {hours}h")

    st.subheader("Behavioral Baselines")
    baselines = db.get_gov_baselines()
    if not baselines:
        st.info("No baselines computed yet. Requires >= 5 samples per metric.")
    else:
        df_bl = pd.DataFrame(baselines)
        _bl_cols = ["agent_role", "metric", "mean", "stddev", "sample_count", "window_days", "computed_at"]
        st.dataframe(df_bl[[c for c in _bl_cols if c in df_bl.columns]], use_container_width=True)

    st.subheader("Anomaly Events")
    anomalies = db.get_gov_anomaly_events(hours=hours, limit=200)
    if not anomalies:
        st.info("No anomalies detected in this window.")
    else:
        _SEV_ICONS  = {"critical": "🔴", "high": "🟠", "medium": "🟡"}
        _SEV_COLORS = {"critical": "#ff4444", "high": "#ff9900", "medium": "#f39c12"}
        for a in anomalies:
            sev      = str(a.get("severity", "medium"))
            metric   = str(a.get("metric", ""))
            agent    = str(a.get("agent_role", "") or "__global__")
            z        = float(a.get("z_score",        0) or 0)
            ts       = str(a.get("ts", ""))[:19]
            icon     = _SEV_ICONS.get(sev, "•")
            with st.expander(
                f"{icon} `{metric}` · z={z:.2f} · **{sev.upper()}** · {agent} · {ts}",
                expanded=(sev == "critical"),
            ):
                ac1, ac2 = st.columns(2)
                ac1.markdown(f"**Metric:** `{metric}`")
                ac1.markdown(f"**Agent:** `{agent}`")
                ac1.markdown(
                    f"**Severity:** <span style='color:{_SEV_COLORS.get(sev,'#888')}'>{sev.upper()}</span>",
                    unsafe_allow_html=True,
                )
                ac2.markdown(f"**Observed:** `{float(a.get('observed_value', 0) or 0):.4f}`")
                ac2.markdown(f"**Baseline:** `{float(a.get('baseline_mean', 0) or 0):.4f}` ± `{float(a.get('baseline_stddev', 0) or 0):.4f}`")
                ac2.markdown(f"**Z-score:** `{z:.3f}`")
                trace_id = str(a.get("trace_id", "") or "")
                if trace_id:
                    st.markdown(f"[Open in Jaeger]({JAEGER_BASE}/trace/{trace_id})")


# =============================================================================
# TAB 8 — Compliance Fit (Agent)
# =============================================================================
with tab_compliance:
    st.subheader("Compliance Fit")
    st.caption("Model registry, DPA/BAA status, and regulatory scope assignments.")

    # Model Registry
    st.subheader("Model Registry")
    mr_data = _gov("/model-registry")
    if mr_data:
        mr_rows = mr_data if isinstance(mr_data, list) else mr_data.get("models", [])
        if mr_rows:
            df_mr = pd.DataFrame(mr_rows)
            # Add DPA/BAA indicators if columns exist
            for col, label in [("dpa_signed", "DPA"), ("baa_signed", "BAA")]:
                if col in df_mr.columns:
                    df_mr[col] = df_mr[col].apply(lambda v: "✅" if v else "❌")
            st.dataframe(df_mr, use_container_width=True)
        else:
            st.info("No models registered yet.")
    else:
        st.info("No model registry data available.")

    with st.form("add_model_registry"):
        st.markdown("**Register model**")
        mr1, mr2, mr3 = st.columns(3)
        model_name    = mr1.text_input("Model name", key="mr_name")
        model_version = mr2.text_input("Model version", key="mr_version")
        model_prov    = mr3.text_input("Provider", key="mr_provider")
        mr4, mr5, mr6 = st.columns(3)
        license_type  = mr4.selectbox("License type", ["proprietary", "open-weight", "apache-2.0", "mit", "cc-by", "gpl"], key="mr_license")
        commercial_ok = mr5.checkbox("Commercial use OK", value=True, key="mr_commercial")
        mr_sectors    = mr6.text_input("Sectors allowed (comma-sep)", key="mr_sectors")
        mr7, mr8      = st.columns(2)
        dpa_signed    = mr7.checkbox("DPA signed", key="mr_dpa")
        baa_signed    = mr8.checkbox("BAA signed", key="mr_baa")
        mr_notes      = st.text_area("Notes", key="mr_notes", height=68)
        if st.form_submit_button("Register Model"):
            if model_name and model_version:
                sectors = [s.strip() for s in mr_sectors.split(",") if s.strip()]
                result  = _gov("/model-registry", method="post", json_body={
                    "model_name": model_name, "model_version": model_version,
                    "provider": model_prov, "license_type": license_type,
                    "commercial_ok": commercial_ok, "dpa_signed": dpa_signed,
                    "baa_signed": baa_signed, "sectors_allowed": sectors,
                    "notes": mr_notes,
                })
                if result:
                    st.success("Model registered.")
                    st.rerun()
            else:
                st.warning("Model name and version are required.")

    st.divider()

    # Regulatory Scope
    st.subheader("Regulatory Scope")
    scope_data = _gov("/regulatory/scope")
    if scope_data:
        sc_rows = scope_data if isinstance(scope_data, list) else scope_data.get("items", [])
        if sc_rows:
            st.dataframe(pd.DataFrame(sc_rows), use_container_width=True)
        else:
            st.info("No regulatory scope entries yet.")
    else:
        st.info("No regulatory scope data available.")

    with st.form("add_reg_scope"):
        st.markdown("**Add regulatory scope**")
        rs1, rs2, rs3 = st.columns(3)
        rs_role   = rs1.text_input("Agent role", key="rs_role")
        rs_fw     = rs2.selectbox("Framework", ["GDPR", "HIPAA", "SOC2", "FINRA", "EU_AI_ACT", "ISO42001"], key="rs_fw")
        rs_sector = rs3.text_input("Sector", key="rs_sector")
        rs4, rs5  = st.columns(2)
        rs_class  = rs4.selectbox(
            "EU AI Act classification (if applicable)",
            ["", "minimal", "limited", "high", "unacceptable"],
            key="rs_class",
        )
        rs_notes  = rs5.text_input("Notes", key="rs_notes")
        if st.form_submit_button("Add Scope"):
            if rs_role:
                result = _gov("/regulatory/scope", method="post", json_body={
                    "agent_role": rs_role, "framework": rs_fw,
                    "sector": rs_sector, "classification": rs_class or None,
                    "notes": rs_notes,
                })
                if result:
                    st.success("Regulatory scope added.")
                    st.rerun()
            else:
                st.warning("Agent role is required.")

    st.divider()

    # Compliance reports (generated reports)
    st.subheader("Compliance Report Generation")
    with st.expander("Generate compliance report", expanded=False):
        rg1, rg2 = st.columns(2)
        from_date = rg1.date_input("From", value=None, key="cf_report_from")
        to_date   = rg2.date_input("To",   value=None, key="cf_report_to")
        gen_by    = st.text_input("Generated by", value="analyst", key="cf_report_gen")
        if st.button("Generate Report", key="cf_gen_report"):
            body = {"generated_by": gen_by}
            if from_date:
                body["from_ts"] = str(from_date) + " 00:00:00"
            if to_date:
                body["to_ts"] = str(to_date) + " 23:59:59"
            result = _gov("/compliance/report", method="post", json_body=body)
            if result:
                score = float(result.get("compliance_score", 0) or 0)
                score_color = "#27ae60" if score >= 80 else ("#ff9900" if score >= 50 else "#ff4444")
                st.markdown(
                    f"Report generated — Score: "
                    f"<span style='color:{score_color};font-size:1.4em;font-weight:bold'>"
                    f"{score:.0f}/100</span>",
                    unsafe_allow_html=True,
                )
                st.json(result)


# =============================================================================
# TAB 9 — Budget & Cost
# =============================================================================
with tab_budget:
    st.subheader("Budget & Cost")
    st.caption("Agent token budget utilization, model routing decisions, and cost savings.")

    budget_rows = db.get_gov_budget_status(hours=hours)

    if not budget_rows:
        st.info("No agent usage recorded today. Run your agents first.")
    else:
        _STATUS_ICONS = {
            "ok":        "🟢",
            "warning":   "🟡",
            "exceeded":  "🔴",
            "no_budget": "⚪",
        }

        for row in budget_rows:
            agent = str(row.get("agent_role",  "unknown"))
            used  = int(row.get("tokens_used",  0) or 0)
            limit = int(row.get("daily_limit",  0) or 0)
            util  = float(row.get("utilization", 0) or 0)
            cost  = float(row.get("cost_usd_today", 0.0) or 0.0)

            if util >= 1.0:
                status = "exceeded"
            elif util >= 0.80:
                status = "warning"
            elif limit > 0:
                status = "ok"
            else:
                status = "no_budget"

            icon    = _STATUS_ICONS.get(status, "⚪")
            bar_pct = min(util, 1.0)

            with st.expander(
                f"{icon} **{agent}** · {used:,} tokens · "
                f"{'no limit' if limit == 0 else f'{util:.0%} of {limit:,}'}",
                expanded=(status in ("exceeded", "warning")),
            ):
                b1, b2, b3 = st.columns(3)
                b1.metric("Tokens used today", f"{used:,}")
                b2.metric("Daily limit",        f"{limit:,}" if limit > 0 else "—")
                b3.metric("Cost (USD)",         f"${cost:.4f}")
                if limit > 0:
                    st.progress(bar_pct, text=f"{util:.1%} utilization")
                    if status == "exceeded":
                        st.error(f"⛔ Budget EXCEEDED by {used - limit:,} tokens")
                    elif status == "warning":
                        st.warning(f"⚠️ Approaching limit — {limit - used:,} tokens remaining")
                    else:
                        st.success(f"Within budget — {limit - used:,} tokens remaining")
                else:
                    st.caption("No budget configured for this agent.")

    st.divider()

    # ── Configure Agent Budgets ───────────────────────────────────────────────
    st.subheader("Configure Agent Budgets")
    st.caption(
        "Set daily token limits and cost caps per agent. "
        "Leave token limit at 0 to remove the limit. "
        "Token pricing rates are configured in the Thresholds tab."
    )

    # Load known agents: union of today's active agents + already-configured agents
    _active_agents  = sorted({r.get("agent_role", "") for r in budget_rows if r.get("agent_role")})
    _configured     = db.get_gov_agent_budgets()
    _cfg_map        = {r["agent_role"]: r for r in _configured}
    _all_agents     = sorted(set(_active_agents) | set(_cfg_map.keys()) - {""})

    # Manual agent name entry for agents not yet seen today
    with st.expander("➕ Add / edit agent budget", expanded=True):
        _col_agent, _col_tok, _col_cost, _col_ena = st.columns([2, 2, 2, 1])

        with _col_agent:
            _agent_options = ["— type or pick —"] + _all_agents
            _sel_agent = st.selectbox("Agent role", _agent_options, key="bgt_agent_sel")
            _custom_agent = st.text_input(
                "Or type agent name",
                placeholder="e.g. orchestrator",
                key="bgt_agent_custom",
            )
            _target_agent = (_custom_agent.strip() or
                             (_sel_agent if _sel_agent != "— type or pick —" else ""))

        # Pre-fill from existing config if an agent is selected
        _existing = _cfg_map.get(_target_agent, {})

        with _col_tok:
            _tok_limit = st.number_input(
                "Daily token limit (0 = unlimited)",
                min_value=0,
                value=int(_existing.get("daily_token_limit", 0) or 0),
                step=1000,
                help="Total prompt + completion tokens allowed per calendar day",
                key="bgt_tok_limit",
            )

        with _col_cost:
            _cost_limit = st.number_input(
                "Daily cost cap (USD, 0 = no cap)",
                min_value=0.0,
                value=float(_existing.get("cost_usd_limit", 0.0) or 0.0),
                step=0.01,
                format="%.4f",
                help="Maximum estimated spend per calendar day",
                key="bgt_cost_limit",
            )

        with _col_ena:
            st.markdown("&nbsp;")  # vertical alignment spacer
            _enabled = st.toggle(
                "Active",
                value=bool(int(_existing["enabled"]) if _existing.get("enabled") is not None else 1),
                key="bgt_enabled",
            )

        _save_col, _del_col, _ = st.columns([1, 1, 3])
        with _save_col:
            if st.button("💾 Save", type="primary", use_container_width=True, key="bgt_save"):
                if not _target_agent:
                    st.warning("Select or type an agent name first.")
                else:
                    try:
                        db.save_gov_agent_budget(
                            agent_role=_target_agent,
                            daily_token_limit=int(_tok_limit),
                            cost_usd_limit=float(_cost_limit),
                            enabled=1 if _enabled else 0,
                        )
                        st.success(f"Budget saved for **{_target_agent}**.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Save failed: {e}")

        with _del_col:
            if _target_agent and _target_agent in _cfg_map:
                if st.button("🗑 Clear", use_container_width=True, key="bgt_del"):
                    try:
                        db.delete_gov_agent_budget(_target_agent)
                        st.success(f"Budget cleared for **{_target_agent}**.")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Clear failed: {e}")

    # Show all configured budgets in a compact table
    if _configured:
        _cfg_enabled = [r for r in _configured if int(r.get("enabled", 0) or 0)]
        _cfg_disabled = [r for r in _configured if not int(r.get("enabled", 0) or 0)]

        _rows = []
        for r in _configured:
            tok  = int(r.get("daily_token_limit", 0) or 0)
            cost = float(r.get("cost_usd_limit", 0.0) or 0.0)
            _rows.append({
                "Agent":             r.get("agent_role", ""),
                "Daily token limit": f"{tok:,}" if tok > 0 else "unlimited",
                "Cost cap (USD)":    f"${cost:.4f}" if cost > 0 else "none",
                "Active":            "✅" if int(r.get("enabled", 0) or 0) else "⏸ Paused",
                "Last updated":      str(r.get("updated_at", ""))[:19],
            })

        st.dataframe(pd.DataFrame(_rows), use_container_width=True, hide_index=True)
    else:
        st.caption("No budgets configured yet.")

    st.divider()

    # Model routing summary
    st.subheader("Model Routing Decisions")
    routing_summary = db.get_gov_routing_summary(hours=hours)
    total_dec   = int(routing_summary.get("total_decisions", 0) or 0)
    avg_saving  = float(routing_summary.get("avg_savings_pct", 0) or 0)
    max_saving  = float(routing_summary.get("max_savings_pct", 0) or 0)

    rs1, rs2, rs3, rs4, rs5, rs6 = st.columns(6)
    rs1.metric("Total decisions",   total_dec)
    rs2.metric("Simple tier",       int(routing_summary.get("simple_count",   0) or 0))
    rs3.metric("Moderate tier",     int(routing_summary.get("moderate_count", 0) or 0))
    rs4.metric("Complex tier",      int(routing_summary.get("complex_count",  0) or 0))
    rs5.metric("Avg savings",       f"{avg_saving:.1f}%")
    rs6.metric("Max savings",       f"{max_saving:.1f}%")

    if total_dec > 0:
        tier_data = {
            "Tier": ["simple", "moderate", "complex", "critical"],
            "Count": [
                int(routing_summary.get("simple_count",   0) or 0),
                int(routing_summary.get("moderate_count", 0) or 0),
                int(routing_summary.get("complex_count",  0) or 0),
                int(routing_summary.get("critical_count", 0) or 0),
            ],
        }
        df_tier = pd.DataFrame(tier_data).set_index("Tier")
        if df_tier["Count"].sum() > 0:
            st.bar_chart(df_tier, height=200)

        routing_rows = db.get_gov_routing_decisions(hours=hours, limit=200)
        if routing_rows:
            st.subheader("Recent Routing Decisions")
            _TIER_ICONS = {
                "simple": "🟢", "moderate": "🟡", "complex": "🟠", "critical": "🔴",
            }
            for row in routing_rows[:50]:
                tier     = str(row.get("complexity_tier", "") or "")
                model    = str(row.get("model",           "") or "")
                trace_id = str(row.get("trace_id",        "") or "")
                savings  = float(row.get("estimated_savings_pct", 0) or 0)
                ts       = str(row.get("ts", ""))[:19]
                icon     = _TIER_ICONS.get(tier, "•")
                with st.expander(
                    f"{icon} `{tier}` → **{model}** · {savings:.1f}% savings · {ts}",
                    expanded=False,
                ):
                    rc1, rc2 = st.columns(2)
                    rc1.markdown(f"**Tier:** `{tier}` | **Model:** `{model}`")
                    rc2.markdown(f"**Savings:** `{savings:.1f}%` | **Trace:** `{trace_id[:20]}`")
    else:
        st.info("No routing decisions recorded yet.")

    st.divider()
    st.subheader("Cache Hit Rate")
    st.info(
        "Cache hit rate tracking not yet implemented. "
        "When prompt caching is instrumented, this metric will appear here automatically."
    )

    st.subheader("Cost Trend")
    cost_trend = db.get_gov_metric_trend("budget_utilization", hours=hours)
    if cost_trend:
        df_ct = pd.DataFrame(cost_trend).rename(
            columns={"hour": "Time", "avg_value": "Avg Budget Utilization"})
        st.line_chart(df_ct.set_index("Time")[["Avg Budget Utilization"]], height=200)
    else:
        st.caption("No cost trend data in this window.")


# =============================================================================
# TAB 10 — Explainability
# =============================================================================
with tab_explain:
    st.subheader("Explainability")
    st.caption("Prompt snapshot coverage, drift history, and decision transparency.")

    # Prompt snapshot coverage
    try:
        snap_rows = db._execute(f"""
            SELECT
                avg(value)   AS avg_coverage,
                count()      AS sample_count
            FROM otel.gov_metric_snapshots
            WHERE metric = 'prompt_snapshot_coverage'
              AND ts >= now() - INTERVAL {int(hours)} HOUR
        """)
        if snap_rows:
            avg_cov = float(snap_rows[0].get("avg_coverage", 0) or 0)
            samples = int(snap_rows[0].get("sample_count", 0) or 0)
            exp1, exp2 = st.columns(2)
            exp1.metric("Avg Snapshot Coverage", f"{avg_cov:.0%}" if samples else "—")
            exp2.metric("Samples in Window", samples)
    except Exception:
        pass

    st.divider()

    # Prompt drift history
    st.subheader("Prompt Drift History")
    drift_summary = db.get_gov_drift_summary()
    ds1, ds2, ds3 = st.columns(3)
    ds1.metric("Templates Tracked", int(drift_summary.get("templates_tracked", 0) or 0))
    ds2.metric("Drift Events",      int(drift_summary.get("drift_events",      0) or 0))
    ds3.metric("Baselines Set",     int(drift_summary.get("baselines_set",     0) or 0))

    drift_events = db.get_gov_prompt_drift(hours=hours)
    if not drift_events:
        st.info("No prompt drift recorded in this window.")
    else:
        templates: dict = {}
        for row in drift_events:
            tid = str(row.get("template_id", "") or "")
            templates.setdefault(tid, []).append(row)

        for template_id, rows in templates.items():
            baseline   = next((r for r in rows if r.get("is_baseline") == 1), None)
            drifts     = [r for r in rows if r.get("drift_detected") == 1]
            has_drift  = len(drifts) > 0
            icon       = "🔴" if has_drift else "🟢"
            with st.expander(
                f"{icon} **{template_id}** · {len(rows)} hash(es) · {len(drifts)} drift event(s)",
                expanded=has_drift,
            ):
                if baseline:
                    st.markdown(f"**Baseline hash:** `{baseline.get('snapshot_hash', '')}`")
                    st.markdown(f"**First seen:** {str(baseline.get('first_seen', ''))[:19]}")
                for row in rows:
                    h     = str(row.get("snapshot_hash",  ""))
                    is_b  = row.get("is_baseline",    0) == 1
                    is_d  = row.get("drift_detected", 0) == 1
                    seen  = int(row.get("seen_count", 1) or 1)
                    last  = str(row.get("last_seen",  ""))[:19]
                    label = " **[baseline]**" if is_b else (" **[DRIFT]**" if is_d else "")
                    color = "#ff4444" if is_d else ("#27ae60" if is_b else "#888")
                    st.markdown(
                        f"<span style='color:{color}'>`{h[:32]}`</span>{label} "
                        f"— seen {seen}x, last {last}",
                        unsafe_allow_html=True,
                    )

    st.divider()
    st.subheader("Model Card Compliance")
    st.info(
        "Model card compliance is tracked in the Model Registry (Tab 8 · Compliance Fit). "
        "Navigate there to view DPA/BAA status and license information for each registered model."
    )

    st.divider()
    st.subheader("Decision Explainability Score")
    st.info(
        "Automated decision explainability scoring (e.g., chain-of-thought audit, "
        "rationale capture) requires LLM judge integration. Not yet automated — "
        "planned for a future release."
    )


# =============================================================================
# TAB 11 — Lifecycle
# =============================================================================
with tab_lifecycle:
    st.subheader("Lifecycle Management")
    st.caption("Change log, version pins, deployment modes, and prompt drift rate.")

    # Change log
    st.subheader("Change Log (last 30)")
    changes = _gov("/lifecycle/changes", params={"limit": 30})
    if changes:
        ch_rows = changes if isinstance(changes, list) else changes.get("items", [])
        if ch_rows:
            st.dataframe(pd.DataFrame(ch_rows), use_container_width=True)
        else:
            st.info("No lifecycle changes recorded.")
    else:
        st.info("No lifecycle change data available.")

    st.divider()

    # Version pins
    st.subheader("Version Pins")
    vp_data = _gov("/lifecycle/versions")
    if vp_data:
        vp_rows = vp_data if isinstance(vp_data, list) else vp_data.get("items", [])
        if vp_rows:
            st.dataframe(pd.DataFrame(vp_rows), use_container_width=True)
        else:
            st.info("No version pins configured.")
    else:
        st.info("No version pin data available.")

    with st.form("add_version_pin"):
        st.markdown("**Add / update version pin**")
        vp1, vp2 = st.columns(2)
        vp_role    = vp1.text_input("Agent role", key="vp_role")
        vp_model   = vp2.text_input("Model name", key="vp_model")
        vp3, vp4   = st.columns(2)
        vp_version = vp3.text_input("Pinned version", key="vp_version")
        vp_mode    = vp4.selectbox("Deployment mode", ["production", "canary", "shadow"], key="vp_mode")
        if st.form_submit_button("Save Version Pin"):
            if vp_role and vp_model and vp_version:
                result = _gov("/lifecycle/versions", method="post", json_body={
                    "agent_role": vp_role, "model_name": vp_model,
                    "pinned_version": vp_version, "deployment_mode": vp_mode,
                })
                if result:
                    st.success("Version pin saved.")
                    st.rerun()
            else:
                st.warning("Agent role, model name, and version are required.")

    st.divider()

    # Prompt drift rate
    st.subheader("Prompt Drift Rate")
    drift_summary = db.get_gov_drift_summary()
    ld1, ld2, ld3 = st.columns(3)
    ld1.metric("Templates Tracked", int(drift_summary.get("templates_tracked", 0) or 0))
    ld2.metric("Drift Events",      int(drift_summary.get("drift_events",      0) or 0))
    ld3.metric("Baselines Set",     int(drift_summary.get("baselines_set",     0) or 0))

    st.divider()
    st.subheader("Eval Gate Pass Rate")
    st.info(
        "Automated eval gate pass rate (e.g., CI eval gates, staged rollout scoring) "
        "requires eval pipeline integration. "
        "Run evals via the Eval Metrics page to populate this data."
    )


# =============================================================================
# TAB 12 — Incidents
# =============================================================================
with tab_incidents:
    st.subheader("Incident Management")
    st.caption("MTTD/MTTC tracking, incident lifecycle, and remediation log.")

    # KPI cards
    inc_sum = _gov("/incidents/summary", params={"hours": hours})
    if inc_sum:
        im1, im2, im3, im4, im5, im6 = st.columns(6)
        _mttd = float(inc_sum.get("mttd_minutes", -1) or -1)
        _mttc = float(inc_sum.get("mttc_minutes", -1) or -1)
        _mttr = float(inc_sum.get("mttr_minutes", -1) or -1)
        im1.metric("MTTD (min)",        f"{_mttd:.1f}" if _mttd >= 0 else "—")
        im2.metric("MTTC (min)",        f"{_mttc:.1f}" if _mttc >= 0 else "—")
        im3.metric("MTTR (min)",        f"{_mttr:.1f}" if _mttr >= 0 else "—")
        im4.metric("Recurrence Rate",   f"{float(inc_sum.get('recurrence_rate',  0) or 0):.2f}")
        im5.metric("Open Incidents",    int(inc_sum.get("open_count",     0) or 0))
        im6.metric("Resolved",          int(inc_sum.get("resolved_count", 0) or 0))
    else:
        st.info("Incident summary not available.")

    st.divider()

    # ── helpers ──────────────────────────────────────────────────────────────
    _SEV_COLORS_INC = {"critical": "#ff4444", "p1": "#ff4444", "high": "#ff9900",
                       "p2": "#ff9900", "medium": "#f39c12", "low": "#27ae60"}

    def _render_incident_rows(rows, show_update: bool = False):
        for inc in rows:
            iid      = str(inc.get("incident_id", "") or "")
            itype    = str(inc.get("incident_type", "") or "")
            sev      = str(inc.get("severity", ""))
            status   = str(inc.get("status",   ""))
            opened   = str(inc.get("opened_at", "") or "")[:19]
            agent    = str(inc.get("agent_role", "") or "")
            trigger  = str(inc.get("trigger_event", "") or "")
            recur_of = str(inc.get("recurrence_of", "") or "")
            root     = str(inc.get("root_cause", "") or "")
            color    = _SEV_COLORS_INC.get(sev.lower(), "#888")

            _detail_raw = inc.get("detail", "") or ""
            _trace_id = ""
            try:
                _det = json.loads(_detail_raw) if isinstance(_detail_raw, str) and _detail_raw else {}
                _trace_id = str(_det.get("trace_id", "") or "")
            except Exception:
                pass

            contained_ts = str(inc.get("contained_at", "") or "")
            resolved_ts  = str(inc.get("resolved_at",  "") or "")

            with st.expander(
                f"[{sev.upper()}] {itype} · {agent} · opened {opened}",
                expanded=(status == "open" and sev.lower() in ("p1", "critical")),
            ):
                ic1, ic2 = st.columns(2)
                ic1.markdown(
                    f"**Severity:** <span style='color:{color};font-weight:bold'>{sev.upper()}</span>",
                    unsafe_allow_html=True,
                )
                ic1.markdown(f"**Type:** `{itype}`")
                ic1.markdown(f"**Agent:** `{agent}`")
                ic2.markdown(f"**Status:** `{status}`")
                ic2.markdown(f"**Opened:** {opened}")
                if contained_ts and not contained_ts.startswith("1970"):
                    ic2.markdown(f"**Contained:** {contained_ts[:19]}")
                if resolved_ts and not resolved_ts.startswith("1970"):
                    ic2.markdown(f"**Resolved:** {resolved_ts[:19]}")

                st.divider()
                st.markdown(f"**Incident ID:** `{iid}`")
                st.markdown(f"**Trigger:** `{trigger}`" if trigger else "**Trigger:** —")
                if _trace_id:
                    st.markdown(f"**Trace ID:** `{_trace_id}`  ← use in Trace Lineage to investigate")
                if recur_of:
                    st.markdown(f"**Recurrence of:** `{recur_of[:24]}`")
                if root:
                    st.markdown(f"**Root cause:** {root}")

                if show_update:
                    st.divider()
                    with st.form(key=f"upd_form_{iid}"):
                        uf1, uf2 = st.columns(2)
                        new_status = uf1.selectbox(
                            "Move to",
                            ["contained", "resolved", "open"],
                            key=f"upd_sel_{iid}",
                        )
                        new_cause = uf2.text_input(
                            "Root cause (optional)",
                            value=root,
                            key=f"upd_cause_{iid}",
                        )
                        if st.form_submit_button("Update"):
                            res = _gov(f"/incidents/{iid}", method="put", json_body={
                                "status": new_status, "root_cause": new_cause,
                            })
                            if res:
                                st.success(f"Marked as **{new_status}** — refreshing…")
                                import time; time.sleep(3)
                                st.rerun()

    # ── Active incidents (open) ───────────────────────────────────────────────
    st.subheader("Active Incidents")
    active_inc = _gov("/incidents", params={"hours": hours, "limit": 50, "status": "open"})
    active_rows = (active_inc if isinstance(active_inc, list)
                   else (active_inc or {}).get("incidents", [])) if active_inc else []
    if active_rows:
        st.caption(f"{len(active_rows)} open incident(s) — expand to investigate or update status.")
        _render_incident_rows(active_rows, show_update=True)
    else:
        st.success("No open incidents.")

    st.divider()

    # ── Contained / Resolved incidents ───────────────────────────────────────
    st.subheader("Contained / Resolved")
    closed_inc = _gov("/incidents", params={"hours": hours * 4, "limit": 50, "status": "contained,resolved"})
    closed_rows = (closed_inc if isinstance(closed_inc, list)
                   else (closed_inc or {}).get("incidents", [])) if closed_inc else []
    if closed_rows:
        st.caption(f"{len(closed_rows)} contained or resolved incident(s) in the last {hours * 4}h.")
        _render_incident_rows(closed_rows, show_update=False)
    else:
        st.info("No contained or resolved incidents yet.")

    st.divider()

    # Remediation log
    st.subheader("Remediation Log")
    rem_log = db.get_gov_remediation_log(hours=hours, limit=100)
    if not rem_log:
        st.info("No remediation actions recorded in this window.")
    else:
        df_rem = pd.DataFrame(rem_log)
        st.dataframe(df_rem, use_container_width=True)


# =============================================================================
# TAB 13 — Regulatory
# =============================================================================
with tab_regulatory:
    st.subheader("Regulatory Compliance")
    st.caption("GDPR · HIPAA · SOC2 · FINRA · EU AI Act · ISO 42001 — scorecard and risk register.")

    _FRAMEWORKS = ["GDPR", "HIPAA", "SOC2", "FINRA", "EU_AI_ACT", "ISO42001"]

    # Compliance Scorecard
    st.subheader("Compliance Scorecard")

    scorecard = _gov("/regulatory/scorecard")
    if scorecard:
        sc_data = scorecard if isinstance(scorecard, list) else scorecard.get("frameworks", [scorecard])

        # Normalise — service might return either a list or a dict keyed by framework
        if isinstance(scorecard, dict) and "frameworks" not in scorecard:
            # flat dict: {"GDPR": {...}, "HIPAA": {...}, ...}
            sc_data = [{"framework": k, **v} for k, v in scorecard.items() if isinstance(v, dict)]

        if sc_data:
            for fw_entry in sc_data:
                fw_name      = str(fw_entry.get("framework", ""))
                score_pct    = float(fw_entry.get("score_pct",       0) or 0)
                total_ctrl   = int(fw_entry.get("total_controls",    0) or 0)
                passing_ctrl = int(fw_entry.get("passing_controls",  0) or 0)

                badge_color = (
                    "#27ae60" if score_pct >= 80
                    else "#ff9900" if score_pct >= 50
                    else "#ff4444"
                )
                fc1, fc2, fc3, fc4, fc5 = st.columns([2, 1, 1, 1, 1])
                fc1.markdown(
                    f"**{fw_name}** "
                    f"<span style='background:{badge_color};color:white;padding:2px 8px;"
                    f"border-radius:4px;font-weight:bold'>{score_pct:.0f}%</span>",
                    unsafe_allow_html=True,
                )
                fc2.markdown(f"**Controls:** {total_ctrl}")
                fc3.markdown(f"**Passing:** {passing_ctrl}")
                fc4.progress(score_pct / 100)
                if fc5.button("Recompute", key=f"recompute_{fw_name}"):
                    updated = _gov(f"/regulatory/scorecard", params={"framework": fw_name})
                    if updated:
                        st.success(f"{fw_name} scorecard recomputed.")
                        st.rerun()

                detail = fw_entry.get("detail") or fw_entry.get("controls") or fw_entry.get("breakdown")
                if detail:
                    with st.expander(f"{fw_name} detail breakdown", expanded=False):
                        if isinstance(detail, list):
                            st.dataframe(pd.DataFrame(detail), use_container_width=True)
                        elif isinstance(detail, dict):
                            st.json(detail)
                        else:
                            st.write(detail)
        else:
            st.info("No scorecard data returned. Configure regulatory scope in Tab 8 first.")
    else:
        sc1, sc2 = st.columns([3, 1])
        sc1.info("Run the scorecard to see compliance scores per framework.")
        if sc2.button("Compute Scorecard", key="reg_compute_scorecard"):
            result = _gov("/regulatory/scorecard")
            if result:
                st.rerun()

    # Per-framework recompute buttons when no data
    st.divider()
    st.markdown("**Recompute individual framework**")
    fw_cols = st.columns(len(_FRAMEWORKS))
    for i, fw in enumerate(_FRAMEWORKS):
        if fw_cols[i].button(fw, key=f"reg_single_{fw}"):
            result = _gov("/regulatory/scorecard", params={"framework": fw})
            if result:
                st.success(f"{fw} recomputed.")
                st.rerun()

    st.divider()

    # Risk Register
    st.subheader("Risk Register")
    risk_data = _gov("/regulatory/risk-register")
    if risk_data:
        rr_rows = risk_data if isinstance(risk_data, list) else risk_data.get("risks", [])
        if rr_rows:
            for risk in rr_rows:
                risk_id    = str(risk.get("risk_id", "") or risk.get("id", ""))
                title      = str(risk.get("title", ""))
                category   = str(risk.get("category", ""))
                likelihood = int(risk.get("likelihood", 0) or 0)
                impact     = int(risk.get("impact", 0) or 0)
                owner      = str(risk.get("owner", "") or "")
                mitigation = str(risk.get("mitigation", "") or "")
                status     = str(risk.get("status", "") or "")
                risk_score = likelihood * impact

                if risk_score <= 4:
                    rr_color = "#27ae60"
                elif risk_score <= 9:
                    rr_color = "#ff9900"
                else:
                    rr_color = "#ff4444"

                with st.expander(
                    f"[score={risk_score}] **{title}** · {category} · owner={owner}",
                    expanded=(risk_score >= 10),
                ):
                    rr1, rr2, rr3 = st.columns(3)
                    rr1.markdown(
                        f"**Risk Score:** <span style='color:{rr_color};font-weight:bold'>"
                        f"{risk_score}</span> (L={likelihood} × I={impact})",
                        unsafe_allow_html=True,
                    )
                    rr2.markdown(f"**Category:** `{category}`")
                    rr3.markdown(f"**Status:** `{status}`")
                    st.markdown(f"**Owner:** {owner}")
                    if mitigation:
                        st.markdown(f"**Mitigation:** {mitigation}")
        else:
            st.info("No risks in the register yet.")
    else:
        st.info("No risk register data available.")

    with st.form("add_risk"):
        st.markdown("**Add risk to register**")
        rk1, rk2 = st.columns(2)
        rk_title    = rk1.text_input("Risk title", key="rk_title")
        rk_category = rk2.text_input("Category (e.g. data, model, ops)", key="rk_category")
        rk3, rk4    = st.columns(2)
        rk_like     = rk3.slider("Likelihood (1-5)", 1, 5, 3, key="rk_like")
        rk_impact   = rk4.slider("Impact (1-5)",     1, 5, 3, key="rk_impact")
        rk_score    = rk_like * rk_impact
        rk_color    = "#27ae60" if rk_score <= 4 else ("#ff9900" if rk_score <= 9 else "#ff4444")
        st.markdown(
            f"**Computed risk score:** "
            f"<span style='color:{rk_color};font-size:1.2em;font-weight:bold'>{rk_score}</span>",
            unsafe_allow_html=True,
        )
        rk5, rk6    = st.columns(2)
        rk_owner    = rk5.text_input("Owner", key="rk_owner")
        rk_status   = rk6.selectbox("Status", ["identified", "mitigating", "accepted", "resolved"], key="rk_status")
        rk_mit      = st.text_area("Mitigation plan", key="rk_mitigation", height=80)
        if st.form_submit_button("Add Risk"):
            if rk_title:
                result = _gov("/regulatory/risk-register", method="post", json_body={
                    "title": rk_title, "category": rk_category,
                    "likelihood": rk_like, "impact": rk_impact,
                    "risk_score": rk_score, "owner": rk_owner,
                    "mitigation": rk_mit, "status": rk_status,
                })
                if result:
                    st.success("Risk added to register.")
                    st.rerun()
            else:
                st.warning("Risk title is required.")


# =============================================================================
# TAB 14 — Threshold Configuration
# =============================================================================
with tab_thresholds:
    st.subheader("Configurable Governance Thresholds")
    st.caption(
        "All numeric thresholds used by governance checks are stored here. "
        "Changes take effect on the next trace evaluation — no restart required."
    )

    thresh_data = _gov("/thresholds")
    if not thresh_data:
        st.info("No threshold config loaded yet.")
    else:
        rows = thresh_data if isinstance(thresh_data, list) else []
        categories = sorted({r.get("category", "") for r in rows if r.get("category")})

        # ── Category filter ───────────────────────────────────────────────
        cat_filter = st.selectbox(
            "Filter by category", ["All"] + categories, key="thr_cat_filter"
        )
        filtered = rows if cat_filter == "All" else [r for r in rows if r.get("category") == cat_filter]

        # ── Display & inline edit table ───────────────────────────────────
        st.markdown(f"**{len(filtered)} thresholds** {'' if cat_filter == 'All' else f'in *{cat_filter}*'}")
        st.divider()

        for row in filtered:
            key         = str(row.get("config_key", ""))
            disp        = str(row.get("display_name", "") or key)
            val         = float(row.get("value", 0) or 0)
            unit        = str(row.get("unit",        "") or "")
            desc        = str(row.get("description", "") or "")
            updated_at  = str(row.get("updated_at",  "") or "")[:19]
            updated_by  = str(row.get("updated_by",  "") or "")

            col_lbl, col_val, col_unit, col_edit, col_reset = st.columns([3, 1.5, 1, 1.2, 1])
            col_lbl.markdown(f"**{disp}**  \n`{key}`  \n<small>{desc}</small>", unsafe_allow_html=True)
            col_val.metric("Value", f"{val:g}")
            col_unit.markdown(f"<br><small>{unit}</small>", unsafe_allow_html=True)

            with col_edit.popover("Edit"):
                with st.form(key=f"thr_edit_{key}"):
                    new_val = st.number_input(
                        f"New value for `{key}`",
                        value=val, format="%.6g",
                        key=f"thr_nv_{key}",
                    )
                    upd_by  = st.text_input("Updated by", value="user", key=f"thr_ub_{key}")
                    if st.form_submit_button("Save"):
                        res = _gov(
                            f"/thresholds/{key}", method="put",
                            json_body={
                                "value": new_val,
                                "display_name": disp,
                                "category": row.get("category", ""),
                                "unit": unit,
                                "description": desc,
                                "updated_by": upd_by,
                            },
                        )
                        if res and res.get("ok"):
                            st.success(f"Updated to {new_val:g}")
                            st.rerun()

            if col_reset.button("Reset", key=f"thr_rst_{key}", help="Reset to system default"):
                res = _gov(f"/thresholds/{key}", method="delete")
                if res:
                    st.success("Reset to default.")
                    st.rerun()

            st.caption(f"Last updated {updated_at} by `{updated_by}`")
            st.divider()

    # ── Bulk reset ────────────────────────────────────────────────────────
    st.subheader("Bulk Actions")
    if st.button("Reset ALL thresholds to defaults", type="secondary"):
        res = _gov("/thresholds/reset-all", method="post")
        if res and res.get("ok"):
            st.success(f"All {res.get('seeded', '?')} thresholds reset to defaults.")
            st.rerun()
