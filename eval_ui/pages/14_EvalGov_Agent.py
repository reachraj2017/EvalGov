"""EvalGov Intelligence Agent — Chat UI with live system-state panel."""

import json
import os
import time
import uuid
from datetime import datetime, timezone

import requests
import streamlit as st

AGENT_URL = os.getenv("EVALGOV_AGENT_URL", "http://localhost:8003")

st.set_page_config(page_title="EvalGov Agent", page_icon="🤖", layout="wide")

# ── Session state init ────────────────────────────────────────────────────────

if "messages" not in st.session_state:
    st.session_state.messages = []
if "session_id" not in st.session_state:
    st.session_state.session_id = str(uuid.uuid4())

# ── Helpers ───────────────────────────────────────────────────────────────────

def _agent_post(path: str, body: dict, timeout: int = 90) -> dict | None:
    try:
        r = requests.post(f"{AGENT_URL}{path}", json=body, timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        return None
    except Exception as exc:
        st.error(f"Agent error: {exc}")
        return None


def _agent_get(path: str, timeout: int = 10) -> dict | None:
    try:
        r = requests.get(f"{AGENT_URL}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError:
        return None
    except Exception:
        return None



# ── Layout ────────────────────────────────────────────────────────────────────

st.title("🤖 EvalGov Intelligence Agent")

# Check agent availability
agent_health = _agent_get("/health", timeout=3)
agent_ok = agent_health is not None

if not agent_ok:
    st.error(
        "⚠️ EvalGov Agent service is not reachable at `" + AGENT_URL + "`. "
        "Make sure the `evalgov-agent` container is running."
    )
    st.stop()

col_chat, col_findings = st.columns([3, 2], gap="large")

# ══════════════════════════════════════════════════════════════════════════════
# LEFT — Chat
# ══════════════════════════════════════════════════════════════════════════════

with col_chat:
    # Top controls
    ctrl1, ctrl2, ctrl3 = st.columns([4, 1, 1])
    ctrl1.markdown("**Chat with your AI governance system in plain English.**  \n"
                   "Ask about status, issues, costs, incidents, traces, or take actions like approving HITL requests.")
    if ctrl2.button("🗑️ Clear Chat", use_container_width=True):
        st.session_state.messages = []
        st.rerun()
    if ctrl3.button("↺ Refresh", use_container_width=True):
        st.rerun()

    st.markdown("---")

    # Message history display
    chat_container = st.container(height=520)
    with chat_container:
        if not st.session_state.messages:
            st.markdown(
                "<div style='color:#888;text-align:center;padding:40px 20px;'>"
                "Ask anything:<br><br>"
                "• <em>What's wrong right now?</em><br>"
                "• <em>Show me all pending HITL requests</em><br>"
                "• <em>What did it cost to run yesterday?</em><br>"
                "• <em>Why is agent X circuit breaker open?</em><br>"
                "• <em>Approve HITL request &lt;id&gt;</em><br>"
                "• <em>What's the trust score for analyzer?</em>"
                "</div>",
                unsafe_allow_html=True,
            )
        for msg in st.session_state.messages:
            with st.chat_message(msg["role"]):
                st.markdown(msg["content"])
                if msg.get("tool_calls"):
                    with st.expander(f"🔧 {len(msg['tool_calls'])} tool(s) called", expanded=False):
                        for tc in msg["tool_calls"]:
                            inp = tc.get("inputs", {})
                            inp_str = ", ".join(f"{k}={v}" for k, v in inp.items()) if inp else "no args"
                            st.code(f"→ {tc['name']}({inp_str})")
                            result_preview = tc.get("result", "")[:300]
                            if result_preview:
                                st.caption(result_preview)

    # Chat input
    if prompt := st.chat_input("Ask anything about your AI systems..."):
        st.session_state.messages.append({"role": "user", "content": prompt})

        # Build history for stateless API (exclude last user msg we just added)
        history_for_api = []
        for m in st.session_state.messages[:-1]:
            history_for_api.append({"role": m["role"], "content": m["content"]})

        with st.spinner("Thinking..."):
            data = _agent_post(
                "/chat",
                {"message": prompt, "history": history_for_api},
                timeout=120,
            )

        if data:
            answer = data.get("response", "")
            tool_calls = data.get("tool_calls", [])
            st.session_state.messages.append({
                "role": "assistant",
                "content": answer,
                "tool_calls": tool_calls,
            })
        else:
            st.session_state.messages.append({
                "role": "assistant",
                "content": "⚠️ Agent service unavailable. Please try again.",
                "tool_calls": [],
            })
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# RIGHT — Live System State Panel (auto-refreshes every 60s)
# ══════════════════════════════════════════════════════════════════════════════

with col_findings:
    st.markdown("### 📡 Live System State")
    st.caption("Auto-refreshes every 60s · 24h window · scroll to see all · max 25 per section")

    @st.fragment(run_every=60)
    def _render_system_state():
        data = _agent_get("/system-state?hours=24")
        if data is None:
            st.error("Could not reach agent service.")
            return

        hitl      = data.get("hitl_queue", [])
        policy    = data.get("policy_violations", [])
        incidents = data.get("incidents", [])
        cbs       = data.get("circuit_breakers", [])

        if not any([hitl, policy, incidents, cbs]):
            st.success("✅ All clear — no active issues in the last 24h.")

        def _row(color: str, line1: str, line2: str) -> str:
            return (
                f"<div style='border-left:3px solid {color};padding:4px 10px;margin-bottom:4px;'>"
                f"{line1}<br><small style='color:#aaa'>{line2}</small></div>"
            )

        # ── HITL Queue ────────────────────────────────────────────────────────
        with st.expander(f"🔔 HITL Queue ({len(hitl)})", expanded=bool(hitl)):
            if not hitl:
                st.caption("No pending HITL requests in the last 24h.")
            else:
                now_utc = datetime.now(timezone.utc)
                with st.container(height=210):
                    for req in hitl:
                        action  = req.get("action_type", "unknown")
                        risk    = req.get("risk_tier", "")
                        trace   = str(req.get("trace_id", "") or "")
                        created = str(req.get("created_at", ""))[:16]
                        try:
                            payload = json.loads(req.get("payload") or "{}")
                        except Exception:
                            payload = {}
                        agent  = payload.get("agent_role", req.get("run_id", "—"))
                        ctx    = payload.get("context", {})
                        metric = ctx.get("metric", "")
                        score  = ctx.get("score", "")
                        thresh = ctx.get("threshold", "")
                        try:
                            dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
                            if not dt.tzinfo:
                                dt = dt.replace(tzinfo=timezone.utc)
                            wait_str = f"{int((now_utc - dt).total_seconds() / 60)}m ago"
                        except Exception:
                            wait_str = created
                        color  = "#ff4b4b" if risk == "critical" else "#ff8800"
                        detail = f"{metric} {score}/{thresh}" if metric else ""
                        tid    = f" · trace:{trace[:8]}" if trace else ""
                        st.markdown(
                            _row(color,
                                 f"<strong>{action}</strong> · {agent}",
                                 f"{wait_str} · {risk}" + (f" · {detail}" if detail else "") + tid),
                            unsafe_allow_html=True,
                        )

        # ── Policy Violations ─────────────────────────────────────────────────
        with st.expander(f"🚫 Policy Violations ({len(policy)})", expanded=bool(policy)):
            if not policy:
                st.caption("No policy blocks in the last 24h.")
            else:
                with st.container(height=210):
                    for p in policy:
                        metric = p.get("metric", "—")
                        value  = p.get("value", "")
                        thresh = p.get("threshold", "")
                        msg    = p.get("message", "")
                        ts     = str(p.get("ts", ""))[:16]
                        trace  = str(p.get("trace_id", "") or "")
                        tid    = f" · trace:{trace[:8]}" if trace else ""
                        st.markdown(
                            _row("#ff4b4b",
                                 f"<strong>{metric}</strong> · {value} / {thresh}",
                                 ts + (f" · {msg[:60]}" if msg else "") + tid),
                            unsafe_allow_html=True,
                        )

        # ── Active Incidents ──────────────────────────────────────────────────
        _sev_c = {"p0": "#ff4b4b", "p1": "#ff8800", "p2": "#ffc107", "p3": "#4b9eff"}
        with st.expander(f"🚨 Active Incidents ({len(incidents)})", expanded=bool(incidents)):
            if not incidents:
                st.caption("No open incidents.")
            else:
                with st.container(height=210):
                    for inc in incidents:
                        sev    = inc.get("severity", "p2")
                        itype  = inc.get("incident_type", "Incident")
                        agent  = inc.get("agent_role", "system")
                        ts     = str(inc.get("opened_at", ""))[:16]
                        iid    = str(inc.get("incident_id", "") or "")
                        detail = (inc.get("detail", "") or inc.get("root_cause", "") or "")[:60]
                        ref    = f" · id:{iid[:8]}" if iid else ""
                        st.markdown(
                            _row(_sev_c.get(sev, "#888"),
                                 f"<strong>[{sev.upper()}] {itype}</strong> · {agent}",
                                 ts + (f" · {detail}" if detail else "") + ref),
                            unsafe_allow_html=True,
                        )

        # ── Circuit Breakers ──────────────────────────────────────────────────
        with st.expander(f"⚡ Circuit Breakers ({len(cbs)})", expanded=bool(cbs)):
            if not cbs:
                st.caption("All circuit breakers closed.")
            else:
                with st.container(height=210):
                    for cb in cbs:
                        agent  = cb.get("agent_role", "unknown")
                        state  = cb.get("state", "open")
                        fails  = cb.get("failure_count", 0)
                        thresh = cb.get("failure_threshold", "?")
                        opened = str(cb.get("updated_at", ""))[:16]
                        reason = (cb.get("quarantine_reason", "") or "")[:60]
                        color  = "#ff4b4b" if state.lower() == "open" else "#ff8800"
                        st.markdown(
                            _row(color,
                                 f"<strong>{agent}</strong> · {state.upper()}",
                                 f"{opened} · {fails}/{thresh} failures" + (f" · {reason}" if reason else "")),
                            unsafe_allow_html=True,
                        )

        st.caption(f"↺ {datetime.now().strftime('%H:%M:%S')}")

    _render_system_state()

    # MCP connection info
    with st.expander("🔌 Connect via MCP (Claude Code / external agents)", expanded=False):
        mcp_sse_url = "http://localhost:8003/mcp/sse"
        st.markdown("**Add to Claude Code** (run once on your machine):")
        st.code(f"claude mcp add evalgov --transport sse {mcp_sse_url}", language="bash")
        st.markdown("**Verify connection:**")
        st.code("claude mcp list", language="bash")
        st.caption(
            "The MCP server is exposed on port 8003. Once connected, Claude Code can call all 39 EvalGov "
            "tools directly. Try: *'what's the system health?'* or *'approve HITL request X'* in your Claude Code session."
        )
        st.markdown("**Session note:** Chat context is maintained for the duration of your browser session. "
                    "Use 🗑️ Clear Chat to start a fresh conversation.")
