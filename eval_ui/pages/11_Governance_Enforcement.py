"""Governance Enforcement — unified enforcement console.

Tab 1: Agent Pre-Execution Enforcement — trust scores, burn rates, rogue detection,
        circuit breakers, pre-execution gate toggle
Tab 2: Content Quality Enforcement — quality gate config and decision log
Tab 3: HITL Approvals — pending approvals and decision history
"""

import os
import time

import requests
import streamlit as st

GOV_URL     = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
JAEGER_BASE = os.getenv("PUBLIC_JAEGER_URL", "http://localhost:16686")

st.set_page_config(page_title="Governance Enforcement", page_icon="🛡️", layout="wide")
st.title("🛡️ Governance Enforcement")

# ── shared helpers ────────────────────────────────────────────────────────────

def _get(path, default=None):
    try:
        r = requests.get(f"{GOV_URL}{path}", timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.warning(f"Could not reach governance service ({path}): {exc}")
        return default

def _post(path, payload=None):
    try:
        r = requests.post(f"{GOV_URL}{path}", json=payload or {}, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"Request failed ({path}): {exc}")
        return None

def _put(path, payload):
    try:
        r = requests.put(f"{GOV_URL}{path}", json=payload, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"Request failed ({path}): {exc}")
        return None

def _delete(path):
    try:
        r = requests.delete(f"{GOV_URL}{path}", timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        st.error(f"Request failed ({path}): {exc}")
        return None

def _tier_badge(tier):
    icons = {"verified": "🟢", "high": "🔵", "medium": "🟡", "low": "🟠", "critical": "🔴"}
    return f"{icons.get(tier, '⚪')} {tier.upper()}"

def _cb_color(state):
    return {"closed": "🟢", "half_open": "🟡", "open": "🔴"}.get(state, "⚪")

def _burn_color(level):
    return {"ok": "🟢", "warning": "🟡", "critical": "🔴"}.get(level, "⚪")

def _action_color(action):
    return {"flag": "🟡", "hold": "🟠", "block": "🔴"}.get(action, "⚪")

def _status_badge(status):
    return {
        "pending":  "🟡 PENDING",
        "approved": "🟢 APPROVED",
        "rejected": "🔴 REJECTED",
        "expired":  "⚫ EXPIRED",
    }.get(status, status.upper())

def _tier_color(tier):
    return {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(tier, "⚪")

def _parse_payload(raw):
    import json
    try:
        return json.loads(raw) if raw else {}
    except Exception:
        return {}

def _render_context(context, action_type):
    if not context:
        st.caption("No context provided.")
        return
    highlights = {}
    rest = {}
    priority_keys = ["query", "prompt", "input", "text", "message", "content",
                     "url", "file", "path", "key", "id", "subject"]
    for k, v in context.items():
        if k in priority_keys:
            highlights[k] = v
        else:
            rest[k] = v
    if not highlights and "args" in rest:
        args = rest.pop("args") or {}
        for k, v in (args.items() if isinstance(args, dict) else {}.items()):
            if k in priority_keys:
                highlights[k] = v
            else:
                rest[k] = v
    if highlights:
        st.markdown("**What the agent is doing:**")
        for k, v in highlights.items():
            st.info(f"**{k.replace('_', ' ').title()}:** {v}")
    if rest:
        st.markdown("**Additional args:**")
        st.json(rest)


# ── look-back window + refresh ────────────────────────────────────────────────

_, col_win, col_ref = st.columns([6, 3, 1])
with col_win:
    hours = st.select_slider(
        "Look-back window (event logs only)",
        options=[1, 6, 12, 24, 48, 72, 168, 336, 504, 720],
        value=24,
        format_func=lambda h: (
            f"Last {h}h" if h < 24
            else f"Last {h // 24}d" if h % 24 == 0
            else f"Last {h}h"
        ),
        help="Applies to: quality gate decision log and HITL decision history. "
             "Pending HITL queue, CB states, and trust scores are always current.",
    )
with col_ref:
    if st.button("↺ Refresh"):
        st.rerun()

# ── tabs ──────────────────────────────────────────────────────────────────────

tab1, tab2, tab3 = st.tabs([
    "⚡ Agent Pre-Execution Enforcement",
    "🔬 Content Quality Enforcement",
    "🧑‍⚖️ HITL Approvals",
])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — Agent Pre-Execution Enforcement
# ══════════════════════════════════════════════════════════════════════════════

with tab1:

    # ── Enforcement mode toggle ───────────────────────────────────────────────
    st.subheader("⚙️ Pre-Execution Enforcement Mode")
    _p2 = _get("/enforcement/phase2/status", {})
    _on = bool((_p2 or {}).get("phase2_enabled", False))

    c1, c2, c3 = st.columns([1, 2, 4])
    with c1:
        st.metric("Mode", "🟢 ACTIVE" if _on else "🔴 OBSERVE ONLY")
    with c2:
        if _on:
            if st.button("⏸ Disable enforcement", type="secondary"):
                if _post("/enforcement/phase2/disable"):
                    st.success("Enforcement disabled — observe-only mode.")
                    time.sleep(0.3); st.rerun()
        else:
            if st.button("▶ Enable enforcement", type="primary"):
                if _post("/enforcement/phase2/enable"):
                    st.success("Enforcement enabled — gate active.")
                    time.sleep(0.3); st.rerun()
    with c3:
        if _on:
            st.info("**ACTIVE** — Circuit breaker state is checked on every gate request. "
                    "Agents with an OPEN circuit breaker have all actions blocked at the gate.")
        else:
            st.warning("**Observe-only** — Governance detects violations but does not block "
                       "actions. Enable to activate circuit breaker gating.")

    st.divider()

    # ── Trust Scores ──────────────────────────────────────────────────────────
    st.subheader("🎯 Agent Trust Scores")
    st.caption("Composite 0–1000 score: identity (25%) · behavior (20%) · compliance (40%) · network (15%).")

    trust_data = _get("/enforcement/trust-scores", []) or []
    if not trust_data:
        st.info("No trust scores yet — computed after each trace is evaluated.")
    else:
        avg = sum(float(r.get("trust_score", 0)) for r in trust_data) / len(trust_data)
        crit = sum(1 for r in trust_data if r.get("trust_tier") == "critical")
        low  = sum(1 for r in trust_data if r.get("trust_tier") in ("critical", "low"))
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Agents", len(trust_data))
        m2.metric("Avg score", f"{avg:.0f} / 1000")
        m3.metric("Critical tier", crit, delta=f"-{crit}" if crit else None, delta_color="inverse")
        m4.metric("Low or worse", low)
        st.markdown("---")
        for row in sorted(trust_data, key=lambda r: float(r.get("trust_score", 0))):
            role = row.get("agent_role", "—")
            score = float(row.get("trust_score", 0))
            tier = str(row.get("trust_tier", "medium"))
            with st.expander(f"{_tier_badge(tier)}  **{role}** — {score:.0f} / 1000",
                             expanded=(tier in ("critical", "low"))):
                e1, e2, e3, e4, e5 = st.columns(5)
                e1.metric("Composite", f"{score:.0f}")
                e2.metric("Identity 25%", f"{float(row.get('identity_score', 0)):.0f}")
                e3.metric("Behavior 20%", f"{float(row.get('behavior_score', 0)):.0f}")
                e4.metric("Compliance 40%", f"{float(row.get('compliance_score', 0)):.0f}")
                e5.metric("Network 15%", f"{float(row.get('network_score', 0)):.0f}")
                st.progress(score / 1000, text=f"Overall: {score:.0f}/1000")
                if row.get("computed_at"):
                    st.caption(f"Last computed: {row.get('computed_at')}")
                if st.button(f"Re-compute — {role}", key=f"recompute_{role}"):
                    with st.spinner("Running enforcement cycle…"):
                        res = _post(f"/enforcement/run-cycle/{role}")
                    if res:
                        st.success(f"Trust: {res.get('trust_score', 0):.0f} · "
                                   f"Tier: {res.get('trust_tier')} · "
                                   f"CB: {res.get('cb_state')}")
                        time.sleep(0.4); st.rerun()
                hist_key = f"ts_hist_{role}"
                if st.button("Show history", key=f"ts_hist_btn_{role}"):
                    st.session_state[hist_key] = not st.session_state.get(hist_key, False)
                if st.session_state.get(hist_key):
                    hist = _get(f"/enforcement/trust-scores/{role}/history?limit=30", [])
                    if hist:
                        import pandas as pd
                        df = pd.DataFrame(hist)
                        df["computed_at"] = pd.to_datetime(df["computed_at"])
                        st.line_chart(df.sort_values("computed_at").set_index("computed_at")["trust_score"])
                    else:
                        st.caption("No history yet.")

    st.divider()

    # ── Burn Rates ────────────────────────────────────────────────────────────
    st.subheader("🔥 Burn Rate Alerts")
    st.caption("Error budget consumption velocity: 1× = sustainable · >2× = warning · >10× = critical.")

    burn_data = _get("/enforcement/burn-rates", []) or []
    if not burn_data:
        st.info("No burn rate data yet.")
    else:
        crit_b = [r for r in burn_data if r.get("alert_level") == "critical"]
        warn_b = [r for r in burn_data if r.get("alert_level") == "warning"]
        b1, b2, b3 = st.columns(3)
        b1.metric("Agents monitored", len(burn_data))
        b2.metric("Warning", len(warn_b), delta=f"+{len(warn_b)}" if warn_b else None, delta_color="inverse")
        b3.metric("Critical", len(crit_b), delta=f"+{len(crit_b)}" if crit_b else None, delta_color="inverse")
        st.markdown("---")
        for row in sorted(burn_data,
                          key=lambda r: max(float(r.get("burn_1h", 0)), float(r.get("burn_6h", 0))),
                          reverse=True):
            role  = row.get("agent_role", "—")
            level = str(row.get("alert_level", "ok"))
            with st.expander(f"{_burn_color(level)}  **{role}** — {level.upper()}", expanded=(level != "ok")):
                br1, br2, br3, br4 = st.columns(4)
                for col, key, label in [
                    (br1, "burn_1h", "1h burn"), (br2, "burn_6h", "6h burn"),
                    (br3, "burn_24h", "24h burn"), (br4, "error_budget_pct", "Budget"),
                ]:
                    val = float(row.get(key, 0))
                    col.metric(label, f"{val:.2f}×" if "burn" in key else f"{val*100:.2f}%")
                if level == "critical":
                    st.error("🚨 Critical burn rate — error budget exhaustion imminent.")
                elif level == "warning":
                    st.warning("⚠️ Elevated burn rate — monitor closely.")

    st.divider()

    # ── Rogue Detection ───────────────────────────────────────────────────────
    st.subheader("🤖 Rogue Agent Detection")
    st.caption("Frequency spike · entropy anomaly · capability violations.")

    rogue_data = _get("/enforcement/rogue-assessments", []) or []
    if not rogue_data:
        st.info("No rogue assessments yet.")
    else:
        crit_r = [r for r in rogue_data if r.get("risk_level") == "critical"]
        high_r = [r for r in rogue_data if r.get("risk_level") == "high"]
        r1, r2, r3 = st.columns(3)
        r1.metric("Agents assessed", len(rogue_data))
        r2.metric("High risk", len(high_r), delta=f"+{len(high_r)}" if high_r else None, delta_color="inverse")
        r3.metric("Critical", len(crit_r), delta=f"+{len(crit_r)}" if crit_r else None, delta_color="inverse")
        st.markdown("---")
        _risk_icon = {"critical": "🔴", "high": "🟠", "medium": "🟡", "low": "🟢"}
        for row in sorted(rogue_data, key=lambda r: float(r.get("composite_score", 0)), reverse=True):
            role      = row.get("agent_role", "—")
            composite = float(row.get("composite_score", 0))
            risk      = str(row.get("risk_level", "low"))
            quarantine= bool(row.get("quarantine_recommended", False))
            label = f"{'⚠️ QUARANTINE RECOMMENDED — ' if quarantine else ''}{_risk_icon.get(risk,'⚪')} **{role}** — {risk.upper()} ({composite:.3f})"
            with st.expander(label, expanded=(risk in ("critical", "high"))):
                rr1, rr2, rr3, rr4 = st.columns(4)
                rr1.metric("Composite", f"{composite:.3f}")
                rr2.metric("Frequency spike", f"{float(row.get('frequency_score',0)):.3f}")
                rr3.metric("Entropy anomaly", f"{float(row.get('entropy_score',0)):.3f}")
                rr4.metric("Capability violations", f"{float(row.get('capability_score',0)):.3f}")
                st.progress(composite, text=f"Rogue score: {composite:.3f}")
                if quarantine:
                    st.error("🚨 Quarantine recommended — check Circuit Breakers below.")
                rh_key = f"rogue_hist_{role}"
                if st.button("Show history", key=f"rogue_hist_btn_{role}"):
                    st.session_state[rh_key] = not st.session_state.get(rh_key, False)
                if st.session_state.get(rh_key):
                    hist = _get(f"/enforcement/rogue-assessments/{role}/history?limit=20", [])
                    if hist:
                        import pandas as pd
                        df = pd.DataFrame(hist)
                        df["assessed_at"] = pd.to_datetime(df["assessed_at"])
                        st.line_chart(df.sort_values("assessed_at").set_index("assessed_at")["composite_score"])
                    else:
                        st.caption("No history yet.")

    st.divider()

    # ── Circuit Breakers ──────────────────────────────────────────────────────
    st.subheader("⚡ Circuit Breakers & Kill Switch")
    st.caption("CLOSED → OPEN → HALF_OPEN. Opens on repeated failures; resets automatically on clean cycle.")

    cb_data = _get("/enforcement/circuit-breakers", []) or []
    if not cb_data:
        st.info("No circuit breaker records yet.")
    else:
        open_c = sum(1 for r in cb_data if r.get("state") == "open")
        half_c = sum(1 for r in cb_data if r.get("state") == "half_open")
        clos_c = sum(1 for r in cb_data if r.get("state") == "closed")
        cc1, cc2, cc3 = st.columns(3)
        cc1.metric("CLOSED (healthy)", clos_c)
        cc2.metric("HALF_OPEN (probing)", half_c, delta=f"+{half_c}" if half_c else None, delta_color="inverse")
        cc3.metric("OPEN (quarantined)", open_c, delta=f"+{open_c}" if open_c else None, delta_color="inverse")
        st.markdown("---")
        for row in sorted(cb_data, key=lambda r: {"open": 0, "half_open": 1, "closed": 2}.get(str(r.get("state", "closed")), 3)):
            role   = str(row.get("agent_role", "—"))
            state  = str(row.get("state", "closed"))
            fails  = int(row.get("failure_count", 0) or 0)
            thresh = int(row.get("failure_threshold", 5) or 5)
            reason = str(row.get("quarantine_reason", "") or "")
            label  = f"{_cb_color(state)}  **{role}** — {state.upper()}" + (f" ({reason})" if reason else "")
            with st.expander(label, expanded=(state != "closed")):
                cb1, cb2, cb3 = st.columns(3)
                cb1.metric("State", state.upper())
                cb2.metric("Failures", fails)
                cb3.metric("Threshold", thresh)
                if reason: st.caption(f"Reason: {reason}")
                if row.get("updated_at"): st.caption(f"Last updated: {row.get('updated_at')}")
                if state == "open":
                    st.error(f"🔴 OPEN — all actions from **{role}** are blocked at the gate.")
                elif state == "half_open":
                    st.warning("🟡 HALF_OPEN probe mode — next clean evaluation will close it.")

                btn1, btn2, btn3 = st.columns(3)
                with btn1:
                    if st.button(f"✅ Reset (close) — {role}", key=f"reset_{role}",
                                 disabled=(state == "closed"),
                                 type="primary" if state != "closed" else "secondary"):
                        if (_post(f"/enforcement/circuit-breakers/{role}/reset") or {}).get("ok"):
                            st.success(f"**{role}** reset to CLOSED.")
                            time.sleep(0.4); st.rerun()
                with btn2:
                    if st.button(f"🟡 Set Half-Open — {role}", key=f"halfopen_{role}",
                                 disabled=(state == "half_open")):
                        if (_post(f"/enforcement/circuit-breakers/{role}/half-open") or {}).get("ok"):
                            st.warning(f"**{role}** set to HALF_OPEN.")
                            time.sleep(0.4); st.rerun()
                with btn3:
                    with st.form(key=f"quarantine_form_{role}"):
                        q_reason = st.text_input("Quarantine reason", value="manual_quarantine", key=f"qr_{role}")
                        if st.form_submit_button(f"🚫 Quarantine — {role}", type="secondary"):
                            if (_post(f"/enforcement/circuit-breakers/{role}/quarantine",
                                      {"reason": q_reason or "manual_quarantine"}) or {}).get("ok"):
                                st.warning(f"**{role}** quarantined.")
                                time.sleep(0.4); st.rerun()

    st.divider()

    # ── On-demand cycle ───────────────────────────────────────────────────────
    st.subheader("⚙️ On-Demand Enforcement Cycle")
    st.caption("Run trust score, rogue assessment, burn rates, and circuit breaker checks for a specific agent now.")
    with st.form("manual_cycle"):
        manual_role = st.text_input("Agent role", placeholder="e.g. searcher")
        if st.form_submit_button("Run cycle") and manual_role:
            with st.spinner(f"Running for {manual_role}…"):
                res = _post(f"/enforcement/run-cycle/{manual_role}")
            if res:
                st.success("Complete")
                d1, d2, d3 = st.columns(3)
                d1.metric("Trust score", f"{res.get('trust_score', 0):.0f} / 1000", delta=res.get("trust_tier"))
                d2.metric("Rogue risk", str(res.get("rogue_risk", "low")).upper(),
                          delta=f"score {res.get('rogue_score', 0):.3f}")
                d3.metric("Circuit breaker", str(res.get("cb_state", "closed")).upper())
                if res.get("quarantine"):
                    st.error("⚠️ Quarantine recommended.")
                if res.get("burn_alert", "ok") != "ok":
                    st.warning(f"Burn rate: {res.get('burn_alert','').upper()} — "
                               f"1h: {res.get('burn_1h',0):.2f}× · "
                               f"6h: {res.get('burn_6h',0):.2f}× · "
                               f"24h: {res.get('burn_24h',0):.2f}×")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — Content Quality Enforcement
# ══════════════════════════════════════════════════════════════════════════════

with tab2:

    # ── Enable / Disable ──────────────────────────────────────────────────────
    st.subheader("⚙️ Quality Gates Mode")
    qg_status  = _get("/quality-gates/status", {})
    qg_enabled = bool((qg_status or {}).get("quality_gates_enabled", False))

    qc1, qc2, qc3 = st.columns([1, 2, 4])
    with qc1:
        st.metric("Mode", "🟢 ACTIVE" if qg_enabled else "🔴 OBSERVE ONLY")
    with qc2:
        if qg_enabled:
            if st.button("⏸ Disable quality gates", type="secondary", key="qg_disable"):
                if _post("/quality-gates/disable"):
                    st.success("Quality gates disabled.")
                    time.sleep(0.3); st.rerun()
        else:
            if st.button("▶ Enable quality gates", type="primary", key="qg_enable"):
                if _post("/quality-gates/enable"):
                    st.success("Quality gates enabled.")
                    time.sleep(0.3); st.rerun()
    with qc3:
        if qg_enabled:
            st.info("**ACTIVE** — LLM judge scores are checked each watcher cycle. "
                    "Scores below threshold trigger flag / hold / block decisions.")
        else:
            st.warning("**Observe-only** — Gate configs exist but are not enforced. Enable to activate.")

    st.divider()

    # ── Gate config ───────────────────────────────────────────────────────────
    st.subheader("Configured Gates")
    gates = (_get("/quality-gates", {}) or {}).get("gates", [])

    if not gates:
        st.info("No gates configured. Add one below.")
    else:
        for g in gates:
            gate_id    = g.get("gate_id", "")
            agent_role = g.get("agent_role", "*")
            metric     = g.get("metric", "")
            threshold  = float(g.get("threshold", 0))
            action     = g.get("action", "flag")
            g_enabled  = int(g.get("enabled", 1))
            description= g.get("description", "")
            label = (f"{_action_color(action)} **{metric}** < {threshold} → `{action.upper()}` "
                     f"| agent: `{agent_role}` {'✅' if g_enabled else '⛔ disabled'}")
            with st.expander(label, expanded=False):
                gc1, gc2, gc3, gc4 = st.columns(4)
                gc1.metric("Metric", metric)
                gc2.metric("Threshold", f"< {threshold}")
                gc3.metric("Action", action.upper())
                gc4.metric("Agent", agent_role)
                if description: st.caption(description)
                with st.form(key=f"edit_gate_{gate_id}"):
                    ef1, ef2, ef3 = st.columns(3)
                    new_thr = ef1.number_input("Threshold", 0.0, 1.0, value=threshold, step=0.05, key=f"thr_{gate_id}")
                    new_act = ef2.selectbox("Action", ["flag","hold","block"],
                                            index=["flag","hold","block"].index(action), key=f"act_{gate_id}")
                    new_ena = ef3.selectbox("Enabled", [1,0], index=0 if g_enabled else 1,
                                            format_func=lambda x: "Yes" if x else "No", key=f"ena_{gate_id}")
                    new_dsc = st.text_input("Description", value=description, key=f"dsc_{gate_id}")
                    sf1, sf2 = st.columns(2)
                    if sf1.form_submit_button("💾 Save"):
                        _put(f"/quality-gates/{gate_id}", {
                            "agent_role": agent_role, "metric": metric,
                            "threshold": new_thr, "action": new_act,
                            "enabled": new_ena, "description": new_dsc,
                        })
                        st.success("Updated."); st.rerun()
                    if sf2.form_submit_button("🗑 Delete", type="secondary"):
                        _delete(f"/quality-gates/{gate_id}"); st.rerun()

    st.divider()
    st.subheader("Add Gate")

    _METRICS = [
        "faithfulness", "hallucination", "relevance", "instruction_following",
        "qa_correctness", "custom_rubric", "toxicity", "bias",
        "coherence", "conciseness", "tool_output_handling",
        "knowledge_retention", "role_adherence",
        "conversation_completeness", "conversation_relevancy",
    ]
    with st.form("add_quality_gate"):
        af1, af2, af3, af4 = st.columns(4)
        new_metric    = af1.selectbox("Metric", _METRICS)
        new_threshold = af2.number_input("Min score threshold", 0.0, 1.0, value=0.5, step=0.05)
        new_action    = af3.selectbox("Action if below threshold", ["flag","hold","block"],
                                      help="flag=log · hold=HITL review · block=critical escalation")
        new_role      = af4.text_input("Agent role (* = all)", value="*")
        new_desc      = st.text_input("Description (optional)")
        if st.form_submit_button("➕ Add gate", type="primary"):
            res = _post("/quality-gates", {
                "agent_role": new_role or "*", "metric": new_metric,
                "threshold": new_threshold, "action": new_action,
                "enabled": 1, "description": new_desc,
            })
            if res:
                st.success(f"Gate added: {new_metric} < {new_threshold} → {new_action}")
                st.rerun()

    st.divider()
    st.subheader("Decision Log")
    _win_label = f"{hours // 24}d" if hours >= 24 else f"{hours}h"
    st.caption(f"Showing decisions in the last {_win_label} (change window at top of page).")
    df1, df2 = st.columns(2)
    f_agent  = df1.text_input("Filter by agent", placeholder="blank = all", key="qg_f_agent")
    f_status = df2.selectbox("Status", ["","pending","reviewed","overridden"], key="qg_f_status")

    qp = f"?hours={hours}&limit=300"
    if f_agent:  qp += f"&agent_role={f_agent}"
    if f_status: qp += f"&status={f_status}"
    decisions = (_get(f"/quality-gates/decisions{qp}", {}) or {}).get("decisions", [])

    if not decisions:
        st.info("No decisions in this window.")
    else:
        flags  = sum(1 for d in decisions if d.get("action") == "flag")
        holds  = sum(1 for d in decisions if d.get("action") == "hold")
        blocks = sum(1 for d in decisions if d.get("action") == "block")
        ds1, ds2, ds3 = st.columns(3)
        ds1.metric("🟡 Flagged", flags)
        ds2.metric("🟠 Held", holds)
        ds3.metric("🔴 Blocked", blocks)
        st.markdown("---")
        for d in decisions:
            action    = d.get("action","flag")
            metric    = d.get("metric","")
            score     = float(d.get("score", 0))
            threshold = float(d.get("threshold", 0))
            agent     = d.get("agent_role","")
            status    = d.get("status","pending")
            trace_id  = d.get("trace_id","")
            created   = str(d.get("created_at",""))[:19]
            st.markdown(
                f"{_action_color(action)} `{action.upper()}` — **{metric}** score `{score:.3f}` "
                f"(threshold `{threshold}`) · `{agent}` · {status} · {created}"
                + (f" · [trace]({JAEGER_BASE}/trace/{trace_id})" if trace_id else "")
            )


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — HITL Approvals
# ══════════════════════════════════════════════════════════════════════════════

with tab3:

    pending_rows = _get("/hitl/queue?status=pending&limit=100", []) or []
    all_rows     = _get(f"/hitl/queue?limit=200&hours={hours}", []) or []
    decided_rows = [r for r in all_rows if r.get("status") != "pending"]

    # ── Pending ───────────────────────────────────────────────────────────────
    st.subheader(f"⏳ Pending Approvals ({len(pending_rows)})")
    st.caption("Agents are blocked and waiting. Approve to let the action proceed; reject to return a governance block.")

    if not pending_rows:
        st.success("No pending requests — all agents are running freely.")
    else:
        for row in pending_rows:
            request_id  = row.get("request_id", "")
            tier        = str(row.get("risk_tier", "medium"))
            action      = str(row.get("action_type", ""))
            created     = str(row.get("created_at", ""))
            trace_id    = str(row.get("trace_id", ""))
            payload     = _parse_payload(row.get("payload", ""))
            agent_role  = payload.get("agent_role", "unknown")
            context     = payload.get("context", {})
            escalations = payload.get("escalation_reasons", [])

            label = (f"{_tier_color(tier)} **{agent_role}** → `{action}` "
                     f"— {tier.upper()} · {created[:19]}")
            with st.expander(label, expanded=True):
                hc1, hc2, hc3 = st.columns(3)
                hc1.metric("Agent", agent_role)
                hc2.metric("Action", action)
                hc3.metric("Risk tier", tier.upper())
                if escalations:
                    st.warning(f"Escalation reasons: {', '.join(escalations)}")
                st.markdown("---")
                _render_context(context, action)
                if trace_id:
                    st.caption(f"Request ID: `{request_id}` · [View trace]({JAEGER_BASE}/trace/{trace_id})")
                else:
                    st.caption(f"Request ID: `{request_id}`")
                st.markdown("---")
                a1, a2 = st.columns(2)
                with a1:
                    with st.form(key=f"approve_{request_id}"):
                        approve_notes = st.text_input("Notes (optional)", key=f"anotes_{request_id}",
                                                       placeholder="Approved — routine operation")
                        reviewer      = st.text_input("Reviewer", key=f"arev_{request_id}",
                                                       placeholder="your name / team")
                        if st.form_submit_button("✅ Approve", type="primary"):
                            res = _put(f"/hitl/{request_id}",
                                       {"status": "approved",
                                        "reviewer": reviewer or "operator",
                                        "notes": approve_notes})
                            if res:
                                st.success(f"Approved — agent will proceed with `{action}`.")
                                st.rerun()
                with a2:
                    with st.form(key=f"reject_{request_id}"):
                        reject_notes = st.text_input("Reason (required)", key=f"rnotes_{request_id}",
                                                      placeholder="Policy violation / not authorised")
                        reviewer_r   = st.text_input("Reviewer", key=f"rrev_{request_id}",
                                                      placeholder="your name / team")
                        if st.form_submit_button("🚫 Reject", type="secondary"):
                            res = _put(f"/hitl/{request_id}",
                                       {"status": "rejected",
                                        "reviewer": reviewer_r or "operator",
                                        "notes": reject_notes or "rejected by operator"})
                            if res:
                                st.error("Rejected — agent will receive a governance block.")
                                st.rerun()

    st.divider()

    # ── Decision history ──────────────────────────────────────────────────────
    st.subheader("📋 Decision History")
    st.caption(f"Approved / rejected decisions in the last {_win_label}. Pending queue above is always unfiltered.")
    if not decided_rows:
        st.info("No decisions in this window.")
    else:
        for row in decided_rows[:30]:
            request_id = row.get("request_id", "")
            tier       = str(row.get("risk_tier", "medium"))
            action     = str(row.get("action_type", ""))
            status     = str(row.get("status", ""))
            reviewer   = str(row.get("reviewer", ""))
            notes      = str(row.get("notes", ""))
            created    = str(row.get("created_at", ""))
            decided    = str(row.get("decided_at", ""))
            payload    = _parse_payload(row.get("payload", ""))
            agent_role = payload.get("agent_role", "unknown")
            context    = payload.get("context", {})
            label = (f"{_status_badge(status)} — **{agent_role}** / `{action}` "
                     f"({tier}) · {decided[:19]}")
            with st.expander(label, expanded=False):
                dh1, dh2, dh3, dh4 = st.columns(4)
                dh1.metric("Agent", agent_role)
                dh2.metric("Action", action)
                dh3.metric("Status", status.upper())
                dh4.metric("Reviewer", reviewer or "—")
                if context:
                    _render_context(context, action)
                if notes: st.caption(f"Notes: {notes}")
                st.caption(f"Requested: {created[:19]} · Decided: {decided[:19]}")
                st.caption(f"Request ID: `{request_id}`")
