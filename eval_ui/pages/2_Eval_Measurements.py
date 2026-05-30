"""
Eval Measurements — consolidated page:
  Conversations | Prompt Analysis | Traces | Eval Metrics | Thresholds
"""

import sys, os

_here    = os.path.dirname(os.path.abspath(__file__))
_ui_root = os.path.abspath(os.path.join(_here, ".."))
if _ui_root not in sys.path:
    sys.path.insert(0, _ui_root)

import pandas as pd
import plotly.express as px
import streamlit as st
from ui_utils import render_time_controls

JAEGER_BASE = os.getenv("PUBLIC_JAEGER_URL", "http://localhost:16686")

st.set_page_config(page_title="Eval Measurements", page_icon="📏", layout="wide")
st.title("📏 Eval Measurements")

try:
    from db import db
    DB_AVAILABLE = True
except Exception as _e:
    DB_AVAILABLE = False
    _db_err = str(_e)

if not DB_AVAILABLE:
    st.error(f"Database connection unavailable: {_db_err}")
    st.stop()

# ── Shared helpers ─────────────────────────────────────────────────────────────

def _trunc(text, n=120):
    s = str(text or "")
    return s[:n] + "…" if len(s) > n else s

def _fmt_ms(ns):
    try:
        return f"{float(ns)/1e6:.1f}"
    except Exception:
        return str(ns)

def _score_style(val):
    try:
        f = float(val)
    except Exception:
        return ""
    if f >= 0.8:
        return "background-color:#d4edda;color:#155724"
    if f >= 0.5:
        return "background-color:#fff3cd;color:#856404"
    return "background-color:#f8d7da;color:#721c24"

# ── Single shared time window (applies to all tabs) ───────────────────────────
_start, _end = render_time_controls("meas")

# ── Token pricing — read once from configurable thresholds ────────────────────
def _load_token_rates() -> tuple[float, float]:
    """Return (input_rate_per_token, output_rate_per_token) from gov_threshold_config."""
    try:
        rows = db._execute(
            "SELECT config_key, value FROM otel.gov_threshold_config FINAL "
            "WHERE config_key IN ('budget.input_token_cost_per_1m', "
            "                     'budget.output_token_cost_per_1m')"
        )
        rate_map = {r["config_key"]: float(r["value"]) for r in (rows or [])}
        return (
            rate_map.get("budget.input_token_cost_per_1m",  0.15) / 1_000_000,
            rate_map.get("budget.output_token_cost_per_1m", 0.60) / 1_000_000,
        )
    except Exception:
        return 0.15 / 1_000_000, 0.60 / 1_000_000

_input_rate, _output_rate = _load_token_rates()

# ── Top-level tabs ─────────────────────────────────────────────────────────────
tab_conv, tab_pa, tab_traces, tab_em, tab_thresh = st.tabs([
    "💬 Conversations",
    "🔭 Prompt Analysis",
    "🔍 Traces",
    "📐 Eval Metrics",
    "🚨 Thresholds",
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — CONVERSATIONS
# ══════════════════════════════════════════════════════════════════════════════
with tab_conv:
    st.subheader("Conversations")
    st.markdown(
        "Browse multi-turn agent conversations. Drill down into individual turns and "
        "view conversation-level evaluation scores."
    )

    st.header("Recent Conversations")
    conv_limit = st.slider("Max conversations", 10, 200, 50, 10, key="conv_limit")

    try:
        convs = db.get_conversations(start=_start, end=_end, limit=conv_limit)
    except Exception as e:
        st.error(f"Could not load conversations: {e}")
        convs = []

    if not convs:
        st.info(
            "No multi-turn conversations found in the selected time window. "
            "Start a chat in the demo UI with conversation tracking enabled."
        )
    else:
        conv_rows = []
        for c in convs:
            services = c.get("services") or []
            conv_rows.append({
                "conversation_id": str(c.get("conversation_id", "")),
                "turns":           int(c.get("turn_count", 0)),
                "first_turn":      str(c.get("first_turn_at", ""))[:19],
                "last_turn":       str(c.get("last_turn_at", ""))[:19],
                "services":        ", ".join(sorted(services)) if services else "",
            })

        conv_df = pd.DataFrame(conv_rows)
        st.dataframe(conv_df, use_container_width=True, hide_index=True)
        st.caption(f"{len(conv_rows)} conversation(s) found.")
        st.divider()

        st.header("Conversation Detail")
        conv_id_options = ["— select a conversation —"] + [c["conversation_id"] for c in conv_rows]
        selected_conv = st.selectbox("Select conversation", conv_id_options, key="conv_selector")

        if selected_conv != "— select a conversation —":
            sel_meta = next((c for c in conv_rows if c["conversation_id"] == selected_conv), {})
            mc1, mc2, mc3 = st.columns(3)
            mc1.metric("Turns",      sel_meta.get("turns", "?"))
            mc2.metric("First turn", sel_meta.get("first_turn", "?"))
            mc3.metric("Last turn",  sel_meta.get("last_turn", "?"))
            st.caption(f"Conversation ID: `{selected_conv}`")

            ctab_turns, ctab_scores = st.tabs(["💬 Turns", "📊 Conversation Scores"])

            with ctab_turns:
                try:
                    turns = db.get_conversation_turns(selected_conv)
                except Exception as e:
                    st.error(f"Could not load turns: {e}")
                    turns = []

                if not turns:
                    st.info("No turns found for this conversation.")
                else:
                    turn_rows = []
                    for i, t in enumerate(turns, start=1):
                        attrs = t.get("SpanAttributes", {}) or {}
                        trace_id = str(t.get("TraceId", ""))
                        turn_rows.append({
                            "turn":        i,
                            "Timestamp":   str(t.get("Timestamp", ""))[:19],
                            "ServiceName": t.get("ServiceName", ""),
                            "agent.role":  attrs.get("agent.role", ""),
                            "task.input":  _trunc(attrs.get("task.input", attrs.get("input", "")), 150),
                            "task.output": _trunc(attrs.get("task.output", attrs.get("output", "")), 150),
                            "duration_ms": _fmt_ms(t.get("Duration", 0)),
                            "StatusCode":  t.get("StatusCode", ""),
                            "trace_id":    trace_id[:20],
                        })

                    st.dataframe(pd.DataFrame(turn_rows), use_container_width=True, hide_index=True)
                    st.caption(f"{len(turn_rows)} turn(s).")

                    st.subheader("Turn Detail")
                    turn_options = [
                        f"Turn {r['turn']} · {r['Timestamp']} · {r['ServiceName']}"
                        for r in turn_rows
                    ]
                    sel_turn_label = st.selectbox(
                        "Inspect turn", ["— pick one —"] + turn_options, key="conv_turn_sel"
                    )
                    if sel_turn_label != "— pick one —":
                        turn_idx = turn_options.index(sel_turn_label)
                        t_detail = turns[turn_idx]
                        t_attrs  = t_detail.get("SpanAttributes", {}) or {}
                        tid      = str(t_detail.get("TraceId", ""))
                        with st.expander("Full task input", expanded=True):
                            st.write(t_attrs.get("task.input", t_attrs.get("input", "—")))
                        with st.expander("Full task output", expanded=True):
                            st.write(t_attrs.get("task.output", t_attrs.get("output", "—")))
                        if tid:
                            st.markdown(f"[🔍 View in Jaeger]({JAEGER_BASE}/trace/{tid})")

            with ctab_scores:
                try:
                    conv_scores = db.get_scores_for_conversation(selected_conv)
                except Exception as e:
                    st.error(f"Could not load conversation scores: {e}")
                    conv_scores = []

                if not conv_scores:
                    st.info(
                        "No conversation-level scores found yet. "
                        "Scores appear after the conversation idles for 60 seconds "
                        "and the multi-turn evaluator runs."
                    )
                else:
                    score_rows = [{
                        "metric":       s.get("metric", ""),
                        "score":        round(float(s.get("score", 0)), 4),
                        "eval_type":    s.get("eval_type", ""),
                        "evaluator":    s.get("evaluator", ""),
                        "reasoning":    _trunc(str(s.get("reasoning", "")), 300),
                        "evaluated_at": str(s.get("evaluated_at", ""))[:19],
                    } for s in conv_scores]
                    score_df = pd.DataFrame(score_rows)
                    st.dataframe(
                        score_df.style.applymap(_score_style, subset=["score"]),
                        use_container_width=True, hide_index=True,
                    )
                    st.caption(f"{len(score_rows)} conversation-level score(s).")
                    if score_rows:
                        st.subheader("Score Summary")
                        cols = st.columns(min(len(score_rows), 3))
                        for i, sr in enumerate(score_rows):
                            cols[i % len(cols)].metric(sr["metric"], f"{sr['score']:.3f}")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — PROMPT ANALYSIS (formerly Prompt Lab X)
# ══════════════════════════════════════════════════════════════════════════════
with tab_pa:
    st.subheader("🔭 Prompt Analysis")
    st.caption("Select a prompt to drill into its trace, eval scores, and prompt history.")

    def _pa_q(sql):
        return db._execute(sql)

    # ── Inline filters (moved out of sidebar) ────────────────────────────────
    with st.expander("Filters", expanded=True):
        pf1, pf2, pf3 = st.columns(3)
        with pf1:
            pa_sel_source = st.selectbox(
                "Source", ["(all)", "production", "benchmark", "exploratory"],
                key="pa_source"
            )
            pa_agents_raw = _pa_q("SELECT DISTINCT agent_name FROM otel.prompt_evals ORDER BY agent_name")
            pa_agent_opts = ["(all)"] + [r["agent_name"] for r in pa_agents_raw]
            pa_sel_agent  = st.selectbox("Agent", pa_agent_opts, key="pa_agent")

        with pf2:
            pa_models_raw = _pa_q("SELECT DISTINCT model FROM otel.prompt_evals WHERE model != '' ORDER BY model")
            pa_model_opts = ["(all)"] + [r["model"] for r in pa_models_raw]
            pa_sel_model  = st.selectbox("Model", pa_model_opts, key="pa_model")
            pa_sel_conv   = st.text_input(
                "Conversation ID contains",
                placeholder="Paste partial or full conversation ID…",
                key="pa_conv_filter",
            )

        with pf3:
            pa_score_metric = st.selectbox("Sort / filter by metric", [
                "(none)", "task_success_rate", "faithfulness", "relevance",
                "instruction_following", "tool_accuracy", "step_efficiency",
                "format_compliance", "handoff_success_rate", "tool_error_rate",
                "tool_retry_rate", "timeout_rate", "error_recovery_rate",
                "context_propagation_fidelity", "trace_completeness_rate",
                "dead_span_rate", "agent_failure_rate",
            ], key="pa_score_metric")
            pa_max_score = st.slider("Max score for selected metric", 0.0, 1.0, 1.0, 0.05, key="pa_max_score")
            pa_limit     = st.slider("Rows to load", 10, 500, 100, 10, key="pa_limit")

    # ── Build query ───────────────────────────────────────────────────────────
    _pa_ts = _start.strftime('%Y-%m-%d %H:%M:%S')
    _pa_te = _end.strftime('%Y-%m-%d %H:%M:%S')
    _pa_where = [f"created_at >= '{_pa_ts}' AND created_at <= '{_pa_te}'"]

    if pa_sel_source != "(all)":
        _pa_where.append(
            f"trace_id IN (SELECT DISTINCT TraceId FROM otel.otel_traces "
            f"WHERE SpanAttributes['trace.source'] = '{pa_sel_source}' AND SpanName = 'agent.task')"
        )
    if pa_sel_agent != "(all)":
        _pa_where.append(f"agent_name = '{pa_sel_agent}'")
    if pa_sel_model != "(all)":
        _pa_where.append(f"model = '{pa_sel_model}'")
    if pa_sel_conv.strip():
        _safe_conv = pa_sel_conv.strip().replace("'", "\\'")
        _pa_where.append(
            f"trace_id IN ("
            f"SELECT DISTINCT TraceId FROM otel.otel_traces "
            f"WHERE SpanAttributes['conversation.id'] LIKE '%%{_safe_conv}%%' "
            f"AND SpanName = 'agent.task')"
        )
    if pa_score_metric != "(none)":
        _pa_where.append(f"scores['{pa_score_metric}'] <= {pa_max_score}")

    _pa_where_clause  = "WHERE " + " AND ".join(_pa_where)
    _pa_order_clause  = (
        f"ORDER BY scores['{pa_score_metric}'] ASC, created_at DESC"
        if pa_score_metric != "(none)" else "ORDER BY created_at DESC"
    )

    pa_rows = _pa_q(f"""
        SELECT prompt_eval_id, prompt_hash, trace_id, span_id, run_id,
               agent_name, model, prompt_text, response_text,
               latency_ms, prompt_tokens, completion_tokens, scores, created_at
        FROM otel.prompt_evals
        {_pa_where_clause}
        {_pa_order_clause}
        LIMIT 1 BY trace_id, span_id
        LIMIT {pa_limit}
    """)

    if not pa_rows:
        st.info("No prompt evaluations yet. Run some queries in the Chat UI.")
    else:
        # Enrich with conversation_id, source, cost
        _pa_trace_ids  = [r["trace_id"] for r in pa_rows if r.get("trace_id")]
        _pa_conv_map: dict  = {}
        _pa_source_map: dict = {}
        _pa_cost_map: dict  = {}
        if _pa_trace_ids:
            try:
                _id_list = ", ".join(f"'{tid}'" for tid in _pa_trace_ids)
                _cr = _pa_q(
                    f"SELECT DISTINCT TraceId, "
                    f"SpanAttributes['conversation.id'] AS conversation_id, "
                    f"SpanAttributes['trace.source'] AS trace_source "
                    f"FROM otel.otel_traces WHERE TraceId IN ({_id_list}) AND SpanName = 'agent.task'"
                )
                _pa_conv_map   = {r["TraceId"]: r["conversation_id"] for r in _cr}
                _pa_source_map = {r["TraceId"]: r["trace_source"]    for r in _cr}
                _cost_rows = _pa_q(
                    f"SELECT TraceId, "
                    f"sum(toUInt64OrZero(SpanAttributes['gen_ai.usage.input_tokens'])) as in_tok, "
                    f"sum(toUInt64OrZero(SpanAttributes['gen_ai.usage.output_tokens'])) as out_tok "
                    f"FROM otel.otel_traces WHERE TraceId IN ({_id_list}) "
                    f"AND (SpanName = 'openai.chat' OR SpanName = 'call_llm' OR SpanName LIKE '%%generate_content%%') "
                    f"GROUP BY TraceId"
                )
                _pa_cost_map = {
                    r["TraceId"]: r["in_tok"] * _input_rate + r["out_tok"] * _output_rate
                    for r in _cost_rows
                }
            except Exception:
                pass

        # Summary banner
        pc1, pc2, pc3, pc4, pc5 = st.columns(5)
        pc1.metric("Prompts shown", len(pa_rows))
        _pa_avg_ms = sum(r.get("latency_ms", 0) or 0 for r in pa_rows) / len(pa_rows)
        pc2.metric("Avg latency", f"{_pa_avg_ms:.0f} ms")
        _pa_total_tok = sum(
            (r.get("prompt_tokens", 0) or 0) + (r.get("completion_tokens", 0) or 0)
            for r in pa_rows
        )
        pc3.metric("Total tokens", f"{_pa_total_tok:,}")
        _pa_total_cost = sum(_pa_cost_map.get(r.get("trace_id") or "", 0.0) for r in pa_rows)
        pc4.metric("Est. total cost", f"${_pa_total_cost:.4f}")
        if pa_score_metric != "(none)":
            _pa_vals = [
                r["scores"].get(pa_score_metric)
                for r in pa_rows
                if (r.get("scores") or {}).get(pa_score_metric) is not None
            ]
            pc5.metric(f"Avg {pa_score_metric}", f"{sum(_pa_vals)/len(_pa_vals):.3f}" if _pa_vals else "—")

        st.divider()

        # Prompt table
        _pa_metric_keys = sorted({k for r in pa_rows for k in (r.get("scores") or {}).keys()})
        _pa_records = []
        for r in pa_rows:
            p_tok  = r.get("prompt_tokens", 0) or 0
            c_tok  = r.get("completion_tokens", 0) or 0
            tid_f  = r.get("trace_id") or ""
            rec    = {
                "created_at":      str(r["created_at"])[:19],
                "agent":           r["agent_name"],
                "source":          _pa_source_map.get(tid_f) or "",
                "model":           r.get("model", ""),
                "conversation_id": (_pa_conv_map.get(tid_f) or "")[:20],
                "total_proc_ms":   r.get("latency_ms", 0),
                "total_tokens":    p_tok + c_tok,
                "est_cost_usd":    round(_pa_cost_map.get(tid_f, 0.0), 6),
                "prompt":          _trunc(r.get("prompt_text"), 120),
                "response":        _trunc(r.get("response_text"), 150),
                "trace_id":        tid_f[:20],
                "prompt_hash":     (r.get("prompt_hash") or "")[:16],
            }
            sc = r.get("scores") or {}
            for mk in _pa_metric_keys:
                val = sc.get(mk)
                rec[mk] = round(val, 3) if val is not None else None
            _pa_records.append(rec)

        pa_df = pd.DataFrame(_pa_records)
        _pa_mcols = [c for c in _pa_metric_keys if c in pa_df.columns]
        _pa_styled = pa_df.style.map(_score_style, subset=_pa_mcols) if _pa_mcols else pa_df

        st.subheader(f"Prompt Evaluations ({len(pa_df)} rows)")
        st.dataframe(_pa_styled, use_container_width=True, height=380, hide_index=True)
        st.divider()

        # Drill-down selector
        st.subheader("Drill Down")
        _pa_options = [
            f"{str(r.get('created_at',''))[:19]}  ·  {r.get('agent_name','')}  ·  "
            f"{_trunc(r.get('prompt_text',''), 60)}  ·  trace: {(r.get('trace_id') or '')[:16]}"
            for r in pa_rows
        ]
        pa_sel_label = st.selectbox(
            "Select a prompt to inspect:", ["— pick one —"] + _pa_options, key="pa_drill_sel"
        )

        if pa_sel_label != "— pick one —":
            _pa_sel_idx  = _pa_options.index(pa_sel_label)
            _pa_sel_row  = pa_rows[_pa_sel_idx]
            _pa_trace_id = _pa_sel_row.get("trace_id", "") or ""
            _pa_ph       = _pa_sel_row.get("prompt_hash", "") or ""
            _pa_p_tok    = _pa_sel_row.get("prompt_tokens", 0) or 0
            _pa_c_tok    = _pa_sel_row.get("completion_tokens", 0) or 0
            _pa_proc_ms  = _pa_sel_row.get("latency_ms", 0) or 0

            hc1, hc2, hc3 = st.columns([3, 3, 2])
            hc1.markdown(f"**Trace ID:** `{_pa_trace_id[:20]}`")
            hc2.markdown(f"**Prompt hash:** `{_pa_ph[:16]}`")
            hc3.markdown(
                f"**{_pa_sel_row.get('agent_name','')}** · "
                f"{_pa_proc_ms:,} ms · {_pa_p_tok+_pa_c_tok:,} tokens"
            )

            _pa_t1, _pa_t2, _pa_t3 = st.tabs(["🔍 Trace Details", "📊 Eval Scores", "📝 Prompt Details"])

            with _pa_t1:
                if not _pa_trace_id:
                    st.info("No trace ID available.")
                else:
                    st.markdown(
                        f"**Trace:** `{_pa_trace_id}` &nbsp;&nbsp; "
                        f"[Open in Jaeger ↗]({JAEGER_BASE}/trace/{_pa_trace_id})"
                    )
                    try:
                        _pa_spans = db.get_trace_spans(_pa_trace_id)
                    except Exception as e:
                        st.error(f"Could not load spans: {e}")
                        _pa_spans = []
                    if not _pa_spans:
                        st.info("No spans found yet — ClickHouse may still be flushing.")
                    else:
                        _pa_span_rows = []
                        for s in _pa_spans:
                            attrs = s.get("SpanAttributes", {}) or {}
                            key_attrs = "; ".join(
                                f"{k}={str(v)[:40]}" for k, v in list(attrs.items())[:5]
                            )
                            _pa_span_rows.append({
                                "SpanName":       s.get("SpanName", ""),
                                "SpanId":         str(s.get("SpanId", ""))[:12] + "…",
                                "ParentSpanId":   (str(s.get("ParentSpanId", ""))[:12] + "…") if s.get("ParentSpanId") else "—",
                                "duration_ms":    _fmt_ms(s.get("Duration", 0)),
                                "StatusCode":     s.get("StatusCode", ""),
                                "Timestamp":      str(s.get("Timestamp", ""))[:19],
                                "key_attributes": _trunc(key_attrs, 120),
                            })
                        st.dataframe(pd.DataFrame(_pa_span_rows), use_container_width=True, hide_index=True)

            with _pa_t2:
                if not _pa_trace_id:
                    st.info("No trace ID available.")
                else:
                    try:
                        _pa_scores = db.get_scores_for_trace(_pa_trace_id)
                    except Exception as e:
                        st.error(f"Could not load scores: {e}")
                        _pa_scores = []
                    if not _pa_scores:
                        st.info("No eval scores found for this trace.")
                    else:
                        _pa_score_rows = [{
                            "metric":    s.get("metric", ""),
                            "score":     round(float(s.get("score", 0)), 4),
                            "eval_type": s.get("eval_type", ""),
                            "evaluator": s.get("evaluator", ""),
                            "reasoning": _trunc(str(s.get("reasoning", "")), 200),
                        } for s in _pa_scores]
                        st.dataframe(
                            pd.DataFrame(_pa_score_rows).style.map(_score_style, subset=["score"]),
                            use_container_width=True, hide_index=True,
                        )

            with _pa_t3:
                if not _pa_ph:
                    st.info("No prompt hash available.")
                else:
                    _pr = _pa_q(f"SELECT prompt_text FROM otel.prompt_evals WHERE prompt_hash = '{_pa_ph}' LIMIT 1")
                    if _pr:
                        with st.expander("Prompt text", expanded=True):
                            st.code(_pr[0]["prompt_text"])
                    _pa_variants = _pa_q(f"""
                        SELECT trace_id, run_id, agent_name, model,
                               response_text, latency_ms, prompt_tokens,
                               completion_tokens, scores, created_at
                        FROM otel.prompt_evals
                        WHERE prompt_hash = '{_pa_ph}'
                        ORDER BY created_at DESC
                    """)
                    if not _pa_variants:
                        st.warning("No runs found for this prompt hash.")
                    else:
                        st.write(f"**{len(_pa_variants)} run(s) of this prompt:**")
                        for i, v in enumerate(_pa_variants):
                            v_p  = v.get("prompt_tokens", 0) or 0
                            v_c  = v.get("completion_tokens", 0) or 0
                            v_ms = v.get("latency_ms", 0) or 0
                            v_tid = v.get("trace_id", "") or ""
                            with st.expander(
                                f"Run {i+1} · {str(v['created_at'])[:19]} · {v['agent_name']} · "
                                f"{v_ms:,} ms · {v_p+v_c:,} tokens",
                                expanded=(i == 0),
                            ):
                                col_a, col_b = st.columns([2, 1])
                                with col_a:
                                    st.markdown("**Response:**")
                                    st.write(v.get("response_text", ""))
                                with col_b:
                                    st.markdown("**Performance:**")
                                    st.markdown(
                                        f"Total processing time: **{v_ms:,} ms**  \n"
                                        f"Total tokens used: **{v_p+v_c:,}**  \n"
                                        f"Prompt tokens: {v_p:,} · Completion tokens: {v_c:,}"
                                    )
                                    sc = v.get("scores") or {}
                                    if sc:
                                        st.markdown("**Scores:**")
                                        sc_df = pd.DataFrame(
                                            [{"metric": k, "score": round(v2, 3)}
                                             for k, v2 in sorted(sc.items())]
                                        )
                                        st.dataframe(
                                            sc_df.style.map(_score_style, subset=["score"]),
                                            hide_index=True, use_container_width=True,
                                        )
                                    st.caption(f"trace: `{v_tid[:20]}…`")
                                    if v_tid:
                                        st.markdown(f"[🔗 View in Jaeger]({JAEGER_BASE}/trace/{v_tid})")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — TRACES
# ══════════════════════════════════════════════════════════════════════════════
with tab_traces:
    st.subheader("Trace Browser")
    st.markdown("Browse recent agent traces, inspect span trees, and view evaluation scores.")

    def _tr_score_color(score: float) -> str:
        if score >= 0.8:   return "background-color: #c6efce; color: #276221"
        elif score >= 0.5: return "background-color: #ffeb9c; color: #9c5700"
        else:              return "background-color: #ffc7ce; color: #9c0006"

    def _tr_style_score(val):
        try:    return _tr_score_color(float(val))
        except: return ""

    def _tr_truncate(text, n=100):
        s = str(text) if text is not None else ""
        return s[:n] + "..." if len(s) > n else s

    def _tr_fmt_s(ns):
        try:    return f"{float(ns)/1e9:.3f}"
        except: return str(ns)

    def _tr_fmt_ms(ns):
        try:    return f"{float(ns)/1e6:.1f}"
        except: return str(ns)

    st.header("Filters")
    tf1, tf2, tf3, tf4 = st.columns([2, 2, 1, 1])
    with tf1:
        tr_service_filter = st.text_input("Service Name contains", placeholder="e.g. orchestrator", key="tr_service")
    with tf2:
        tr_role_filter = st.text_input("Agent Role contains", placeholder="e.g. researcher", key="tr_role")
    with tf3:
        tr_source_filter = st.selectbox("Source", ["all", "production", "benchmark", "exploratory"], key="tr_source")
    with tf4:
        tr_limit = st.slider("Max traces", 10, 100, 50, 10, key="tr_limit")

    tr_conv_filter = st.text_input(
        "Conversation ID contains",
        placeholder="Paste partial or full conversation ID…",
        key="tr_conv_filter",
    )

    st.header("Recent Traces")
    try:
        raw_traces = db.get_recent_traces(limit=tr_limit, start=_start, end=_end)
        if not raw_traces:
            st.info("No agent.task traces found in otel_traces.")
        else:
            tr_rows = []
            for t in raw_traces:
                attrs    = t.get("SpanAttributes", {}) or {}
                trace_id = str(t.get("TraceId", ""))
                conv_id  = attrs.get("conversation.id", "")
                tr_rows.append({
                    "trace_id":       trace_id,
                    "TraceId (link)": f"[{trace_id[:16]}...]({JAEGER_BASE}/trace/{trace_id})",
                    "conversation.id": conv_id,
                    "source":         attrs.get("trace.source", "production"),
                    "SpanName":       t.get("SpanName", ""),
                    "ServiceName":    t.get("ServiceName", ""),
                    "agent.role":     attrs.get("agent.role", ""),
                    "task.input":     _tr_truncate(attrs.get("task.input", attrs.get("input", "")), 100),
                    "duration_s":     _tr_fmt_s(t.get("Duration", 0)),
                    "StatusCode":     t.get("StatusCode", ""),
                    "Timestamp":      str(t.get("Timestamp", "")),
                })

            tr_df = pd.DataFrame(tr_rows)
            if tr_service_filter.strip():
                tr_df = tr_df[tr_df["ServiceName"].str.contains(tr_service_filter.strip(), case=False, na=False)]
            if tr_role_filter.strip():
                tr_df = tr_df[tr_df["agent.role"].str.contains(tr_role_filter.strip(), case=False, na=False)]
            if tr_source_filter != "all":
                tr_df = tr_df[tr_df["source"] == tr_source_filter]
            if tr_conv_filter.strip():
                tr_df = tr_df[tr_df["conversation.id"].str.contains(tr_conv_filter.strip(), case=False, na=False)]

            display_cols = [
                "TraceId (link)", "conversation.id", "source", "SpanName", "ServiceName",
                "agent.role", "task.input", "duration_s", "StatusCode", "Timestamp",
            ]
            st.markdown(tr_df[display_cols].to_markdown(index=False), unsafe_allow_html=False)
            st.caption(f"Showing {len(tr_df)} trace(s).")
    except Exception as e:
        st.error(f"Could not load traces: {e}")

    st.divider()
    st.header("Trace Detail")
    tr_id_input = st.text_input(
        "Enter Trace ID to inspect",
        placeholder="Paste full trace ID here…",
        key="tr_detail_input",
    )

    if tr_id_input.strip():
        tid = tr_id_input.strip()
        with st.expander(f"Spans for trace `{tid[:16]}...`", expanded=True):
            try:
                spans = db.get_trace_spans(tid)
                if not spans:
                    st.info("No spans found for this trace ID.")
                else:
                    span_rows = []
                    for s in spans:
                        attrs = s.get("SpanAttributes", {}) or {}
                        key_attrs = "; ".join(
                            f"{k}={str(v)[:40]}" for k, v in list(attrs.items())[:5]
                        )
                        span_rows.append({
                            "SpanName":       s.get("SpanName", ""),
                            "SpanId":         str(s.get("SpanId", ""))[:12] + "...",
                            "ParentSpanId":   (str(s.get("ParentSpanId", ""))[:12] + "...") if s.get("ParentSpanId") else "—",
                            "duration_ms":    _tr_fmt_ms(s.get("Duration", 0)),
                            "StatusCode":     s.get("StatusCode", ""),
                            "Timestamp":      str(s.get("Timestamp", "")),
                            "key_attributes": _tr_truncate(key_attrs, 120),
                        })
                    st.dataframe(pd.DataFrame(span_rows), use_container_width=True, hide_index=True)
            except Exception as e:
                st.error(f"Could not load spans: {e}")

        st.subheader("Evaluation Scores for This Trace")
        try:
            tr_scores = db.get_scores_for_trace(tid)
            if not tr_scores:
                st.info("No eval scores found for this trace.")
            else:
                tr_score_rows = [{
                    "metric":    s.get("metric", ""),
                    "score":     round(float(s.get("score", 0)), 4),
                    "eval_type": s.get("eval_type", ""),
                    "evaluator": s.get("evaluator", ""),
                    "reasoning": _tr_truncate(str(s.get("reasoning", "")), 200),
                } for s in tr_scores]
                st.dataframe(
                    pd.DataFrame(tr_score_rows).style.applymap(_tr_style_score, subset=["score"]),
                    use_container_width=True, hide_index=True,
                )
        except Exception as e:
            st.error(f"Could not load scores: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — EVAL METRICS
# ══════════════════════════════════════════════════════════════════════════════
with tab_em:
    st.subheader("📐 Evaluation Metrics Dashboard")

    _em_time_traces = f"AND Timestamp >= '{_start.strftime('%Y-%m-%d %H:%M:%S')}' AND Timestamp <= '{_end.strftime('%Y-%m-%d %H:%M:%S')}'"
    _em_time_scores = f"AND evaluated_at >= '{_start.strftime('%Y-%m-%d %H:%M:%S')}' AND evaluated_at <= '{_end.strftime('%Y-%m-%d %H:%M:%S')}'"

    _em_agents_raw = db._execute("SELECT DISTINCT agent_name FROM otel.prompt_evals ORDER BY agent_name")
    _em_agent_opts = ["(all)"] + [r["agent_name"] for r in _em_agents_raw]
    _em_models_raw = db._execute("SELECT DISTINCT model FROM otel.prompt_evals WHERE model != '' ORDER BY model")
    _em_model_opts = ["(all)"] + [r["model"] for r in _em_models_raw]

    em_f1, em_f2, em_f3 = st.columns(3)
    with em_f1:
        em_sel_source = st.selectbox("Source", ["all", "production", "benchmark", "exploratory"], key="em_source")
    with em_f2:
        em_sel_agent  = st.selectbox("Agent", _em_agent_opts, key="em_agent")
    with em_f3:
        em_sel_model  = st.selectbox("Model", _em_model_opts, key="em_model")

    _em_src_traces = "" if em_sel_source == "all" else f"AND SpanAttributes['trace.source'] = '{em_sel_source}'"
    _em_src_scores = (
        "" if em_sel_source == "all"
        else (
            f"AND trace_id IN ("
            f"SELECT DISTINCT TraceId FROM otel.otel_traces "
            f"WHERE SpanAttributes['trace.source'] = '{em_sel_source}' AND SpanName = 'agent.task')"
        )
    )
    _em_agent_scores = (
        "" if em_sel_agent == "(all)"
        else f"AND trace_id IN (SELECT DISTINCT trace_id FROM otel.prompt_evals WHERE agent_name = '{em_sel_agent}')"
    )
    _em_model_scores = (
        "" if em_sel_model == "(all)"
        else f"AND trace_id IN (SELECT DISTINCT trace_id FROM otel.prompt_evals WHERE model = '{em_sel_model}')"
    )
    # Trace-level filters for otel_traces queries (agent and model resolved via trace IDs)
    _em_agent_traces = (
        "" if em_sel_agent == "(all)"
        else f"AND TraceId IN (SELECT DISTINCT TraceId FROM otel.otel_traces WHERE SpanAttributes['agent.role'] = '{em_sel_agent}' AND SpanName = 'agent.task')"
    )
    _em_model_traces = (
        "" if em_sel_model == "(all)"
        else f"AND TraceId IN (SELECT DISTINCT trace_id FROM otel.prompt_evals WHERE model = '{em_sel_model}')"
    )

    st.caption(f"Showing: source=**{em_sel_source}**  agent=**{em_sel_agent}**  model=**{em_sel_model}**")

    def _em_ft(extra=""):
        parts = [p for p in [_em_time_traces, _em_src_traces, _em_agent_traces, _em_model_traces, extra] if p]
        return " ".join(parts)

    def _em_fs(extra=""):
        parts = [p for p in [_em_time_scores, _em_src_scores, _em_agent_scores, _em_model_scores, extra] if p]
        return " ".join(parts)

    def _em_fetch(sql):
        try:
            rows = db._execute(sql)
            return float(rows[0]["value"]) if rows else 0.0
        except Exception:
            return 0.0

    def _em_pct(v):   return f"{v*100:.1f}%"
    def _em_ratio(v): return f"{v:.3f}"
    def _em_ms(v):    return f"{v:.1f} ms"
    def _em_sec(v):   return f"{v:.2f} s"
    def _em_count(v): return f"{v:.1f}"

    def _em_score_color(v, hib=True):
        eff = v if hib else 1.0 - v
        if eff >= 0.8: return "background:#d4edda;color:#155724"
        if eff >= 0.5: return "background:#fff3cd;color:#856404"
        if eff > 0.0:  return "background:#f8d7da;color:#721c24"
        return "background:#e2e3e5;color:#383d41"

    def _em_card(col, num, name, value, fmt_fn, hib=True, ni=False):
        if ni:
            display = "—"
            style   = "background:#e2e3e5;color:#383d41"
            cap     = "<div style='font-size:10px;opacity:.6;margin-top:4px'>Needs instrumentation</div>"
        else:
            display = fmt_fn(value)
            style   = _em_score_color(value, hib)
            cap     = ""
        with col:
            st.html(
                f"<div style='border-radius:8px;padding:12px 10px;text-align:center;{style};margin-bottom:6px'>"
                f"<div style='font-size:11px;opacity:.7;margin-bottom:2px'>{num}</div>"
                f"<div style='font-size:13px;font-weight:600;line-height:1.3'>{name}</div>"
                f"<div style='font-size:22px;font-weight:700;margin-top:6px'>{display}</div>"
                f"{cap}</div>"
            )

    def _em_section(title, metrics):
        with st.expander(title, expanded=True):
            cols = st.columns(6)
            for i, m in enumerate(metrics):
                _em_card(
                    cols[i % 6], m["num"], m["name"], m.get("value", 0.0),
                    m["fmt"], m.get("hib", True), m.get("ni", False),
                )

    # Fetch values
    _em_LLM = "(SpanName = 'openai.chat' OR SpanName = 'call_llm' OR SpanName LIKE '%%generate_content%%' OR SpanName LIKE '%%ChatCompletion%%')"
    ev = {}
    ev[1]  = _em_fetch(f"SELECT IF(count()>0, countIf(StatusCode != 'STATUS_CODE_ERROR') / count(), 0) as value FROM otel.otel_traces WHERE SpanName = 'agent.task' {_em_ft()}")
    ev[3]  = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'faithfulness' {_em_fs()}")
    ev[4]  = _em_fetch(f"SELECT IF(count()>0, 1.0 - avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'faithfulness' {_em_fs()}")
    ev[5]  = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'relevance' {_em_fs()}")
    ev[6]  = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'instruction_following' {_em_fs()}")
    ev["qa"]     = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'qa_correctness' {_em_fs()}")
    ev["rubric"] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'custom_rubric' {_em_fs()}")
    ev["halluc"] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'hallucination_score' {_em_fs()}")
    ev["toxicity"] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'toxicity_score' {_em_fs()}")
    ev["bias"]     = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'bias_score' {_em_fs()}")
    ev["cohere"]   = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'coherence' {_em_fs()}")
    ev["concise"]  = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'conciseness' {_em_fs()}")
    ev[8]  = _em_fetch(f"SELECT IF(count()>0, avg(Duration)/1e9, 0) as value FROM otel.otel_traces WHERE SpanName = 'agent.task' {_em_ft()}")
    ev[9]  = _em_fetch(f"""
        SELECT IF(count()>0, avg(per_trace_cost), 0) as value FROM (
            SELECT TraceId,
                sum(COALESCE(toFloat64OrZero(SpanAttributes['llm.usage.total_cost_usd']), 0) +
                    IF(toFloat64OrZero(SpanAttributes['llm.usage.total_cost_usd']) = 0,
                        toUInt64OrZero(SpanAttributes['gen_ai.usage.input_tokens'])  * {_input_rate} +
                        toUInt64OrZero(SpanAttributes['gen_ai.usage.output_tokens']) * {_output_rate}, 0)
                ) as per_trace_cost
            FROM otel.otel_traces WHERE {_em_LLM} {_em_ft()}
            GROUP BY TraceId HAVING per_trace_cost > 0)
    """)
    ev[10] = _em_fetch(f"SELECT IF(count()>0, avg(Duration)/1e6, 0) as value FROM otel.otel_traces WHERE {_em_LLM} {_em_ft()}")
    ev[11] = _em_fetch(f"SELECT IF(count()>0, avg(Duration)/1e9, 0) as value FROM otel.otel_traces WHERE SpanName = 'agent.task' {_em_ft()}")
    ev[12] = _em_fetch(f"SELECT IF(count()>0, avg(toUInt64OrZero(SpanAttributes['gen_ai.usage.input_tokens']) + toUInt64OrZero(SpanAttributes['gen_ai.usage.output_tokens'])), 0) as value FROM otel.otel_traces WHERE {_em_LLM} {_em_ft()}")
    ev[13] = _em_fetch(f"SELECT IF(countDistinct(TraceId)>0, count() / countDistinct(TraceId), 0) as value FROM otel.otel_traces WHERE SpanName = 'agent.tool_call' {_em_ft()}")
    ev[15] = _em_fetch(f"SELECT IF(count()>0, countIf(StatusCode != 'STATUS_CODE_ERROR') / count(), 0) as value FROM otel.otel_traces WHERE SpanName = 'agent.handoff' {_em_ft()}")
    ev[16] = ev[6]; ev[17] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'context_propagation_fidelity' {_em_fs()}")
    ev[18] = ev[6]
    ev[21] = _em_fetch(f"SELECT IF(count()>0, avg(Duration)/1e6, 0) as value FROM otel.otel_traces WHERE SpanName = 'agent.handoff' {_em_ft()}")
    ev[23] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'tool_accuracy' {_em_fs()}")
    ev[29] = _em_fetch(f"SELECT IF(count()>0, avg(toUInt64OrZero(SpanAttributes['gen_ai.usage.input_tokens'])) / 128000, 0) as value FROM otel.otel_traces WHERE {_em_LLM} {_em_ft()}")
    ev[30] = _em_fetch(f"SELECT IF(count()>0, countIf(SpanAttributes['tool.success'] = 'true') / count(), 0) as value FROM otel.otel_traces WHERE SpanName = 'agent.tool_call' {_em_ft()}")
    ev[35] = _em_fetch(f"SELECT IF(count()>0, 1.0 - avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'tool_retry_rate' {_em_fs()}")
    ev["tool_sel"] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'tool_selection_accuracy' {_em_fs()}")
    ev["tool_arg"] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'tool_argument_accuracy' {_em_fs()}")
    ev["tool_out"] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'tool_output_handling' {_em_fs()}")
    ev[37] = _em_fetch(f"SELECT IF(count()>0, countIf(StatusCode = 'STATUS_CODE_ERROR') / count(), 0) as value FROM otel.otel_traces WHERE SpanName = 'agent.task' {_em_ft()}")
    ev[38] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'error_recovery_rate' {_em_fs()}")
    ev[42] = _em_fetch(f"SELECT IF(count()>0, countIf(Duration > 30000000000) / count(), 0) as value FROM otel.otel_traces WHERE SpanName = 'agent.task' {_em_ft()}")
    ev[47] = _em_fetch(f"SELECT IF(count()>0, countIf(toUInt64OrZero(SpanAttributes['gen_ai.usage.input_tokens']) > 102400) / count(), 0) as value FROM otel.otel_traces WHERE {_em_LLM} {_em_ft()}")
    ev[50] = _em_fetch(f"SELECT IF(countDistinct(TraceId)>0, countDistinct(if(SpanName='agent.task', TraceId, NULL)) / countDistinct(TraceId), 0) as value FROM otel.otel_traces WHERE 1=1 {_em_ft()}")
    ev[51] = _em_fetch(f"SELECT IF(count()>0, countIf(ParentSpanId != '') / count(), 0) as value FROM otel.otel_traces WHERE 1=1 {_em_ft()}")
    ev[55] = _em_fetch(f"SELECT IF(count()>0, avg(toUnixTimestamp(now()) - toUnixTimestamp(Timestamp)), 0) as value FROM otel.otel_traces WHERE Timestamp >= now() - INTERVAL 1 HOUR {_em_ft()}")
    ev[56] = _em_fetch(f"""
        SELECT IF(count()>0, countIf(ParentSpanId != '' AND NOT has(groupArray(SpanId), ParentSpanId)) / count(), 0) as value
        FROM (SELECT SpanId, ParentSpanId FROM otel.otel_traces WHERE TraceId IN (
            SELECT TraceId FROM otel.otel_traces WHERE 1=1 {_em_ft()} LIMIT 1000))
    """)
    _em_mt = "AND eval_type = 'multiturn_judge'"
    ev["know"]  = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'knowledge_retention' {_em_mt} {_em_fs()}")
    ev["role"]  = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'role_adherence' {_em_mt} {_em_fs()}")
    ev["comp"]  = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'conversation_completeness' {_em_mt} {_em_fs()}")
    ev["relev"] = _em_fetch(f"SELECT IF(count()>0, avg(score), 0) as value FROM otel.eval_scores WHERE metric = 'conversation_relevancy' {_em_mt} {_em_fs()}")

    _em_section("1 — Task Completion & Accuracy  (10 metrics)", [
        {"num":"01","name":"Task Success Rate",         "value":ev[1],         "fmt":_em_pct,   "hib":True},
        {"num":"02","name":"Subtask Completion Rate",   "value":0.0,           "fmt":_em_pct,   "hib":True,  "ni":True},
        {"num":"03","name":"Goal Achievement Fidelity", "value":ev[3],         "fmt":_em_ratio, "hib":True},
        {"num":"04","name":"Hallucination Rate",        "value":ev[4],         "fmt":_em_ratio, "hib":False},
        {"num":"05","name":"Output Correctness Score",  "value":ev[5],         "fmt":_em_ratio, "hib":True},
        {"num":"06","name":"Instruction Adherence",     "value":ev[6],         "fmt":_em_ratio, "hib":True},
        {"num":"07","name":"False Refusal Rate",        "value":0.0,           "fmt":_em_pct,   "hib":False, "ni":True},
        {"num":"A1","name":"Q&A Correctness",           "value":ev["qa"],      "fmt":_em_ratio, "hib":True},
        {"num":"A2","name":"Custom Rubric Score",       "value":ev["rubric"],  "fmt":_em_ratio, "hib":True},
        {"num":"A3","name":"Hallucination Score",       "value":ev["halluc"],  "fmt":_em_ratio, "hib":True},
        {"num":"A4","name":"Conciseness",               "value":ev["concise"], "fmt":_em_ratio, "hib":True},
    ])
    _em_section("2 — Efficiency & Cost  (7 metrics)", [
        {"num":"08","name":"E2E Task Latency",       "value":ev[8],  "fmt":_em_sec,               "hib":False},
        {"num":"09","name":"Cost per Task (USD)",     "value":ev[9],  "fmt":lambda x:f"${x:.4f}",  "hib":False},
        {"num":"10","name":"Time to First Token",    "value":ev[10], "fmt":_em_ms,                "hib":False},
        {"num":"11","name":"Agent Hop Latency",      "value":ev[11], "fmt":_em_sec,               "hib":False},
        {"num":"12","name":"Token Usage per Task",   "value":ev[12], "fmt":_em_count,             "hib":False},
        {"num":"13","name":"Tool Calls per Task",    "value":ev[13], "fmt":_em_count,             "hib":False},
        {"num":"14","name":"Token Efficiency Ratio", "value":0.0,    "fmt":_em_ratio, "hib":True, "ni":True},
    ])
    _em_section("3 — Multi-Agent Coordination  (8 metrics)", [
        {"num":"15","name":"Handoff Success Rate",        "value":ev[15],"fmt":_em_pct,   "hib":True},
        {"num":"16","name":"Delegation Accuracy",         "value":ev[16],"fmt":_em_ratio, "hib":True},
        {"num":"17","name":"Context Propagation Fidelity","value":ev[17],"fmt":_em_ratio, "hib":True},
        {"num":"18","name":"Orchestrator Plan Quality",   "value":ev[18],"fmt":_em_ratio, "hib":True},
        {"num":"19","name":"Conflict Resolution Rate",    "value":0.0,   "fmt":_em_pct,   "hib":True,  "ni":True},
        {"num":"20","name":"Circular Dependency Rate",    "value":0.0,   "fmt":_em_pct,   "hib":False, "ni":True},
        {"num":"21","name":"Inter-Agent Comm Latency",    "value":ev[21],"fmt":_em_ms,    "hib":False},
        {"num":"22","name":"Idle Agent Time Ratio",       "value":0.0,   "fmt":_em_pct,   "hib":False, "ni":True},
    ])
    _em_section("4 — Reasoning & Decision Quality  (7 metrics)", [
        {"num":"23","name":"Tool Selection Precision",     "value":ev[23],"fmt":_em_ratio, "hib":True},
        {"num":"24","name":"Reasoning Chain Coherence",    "value":ev["cohere"], "fmt":_em_ratio, "hib":True},
        {"num":"25","name":"Re-planning Rate",             "value":0.0,   "fmt":_em_pct,   "hib":False, "ni":True},
        {"num":"26","name":"Dead-end Detection Rate",      "value":0.0,   "fmt":_em_pct,   "hib":True,  "ni":True},
        {"num":"27","name":"Confidence Calibration (ECE)", "value":0.0,   "fmt":_em_ratio, "hib":False, "ni":True},
        {"num":"28","name":"Backtrack Rate",               "value":0.0,   "fmt":_em_pct,   "hib":False, "ni":True},
        {"num":"29","name":"Context Window Utilization",   "value":ev[29],"fmt":_em_pct,   "hib":False},
    ])
    _em_section("5 — Tool & MCP Integration  (10 metrics)", [
        {"num":"30","name":"Tool Call Success Rate",  "value":ev[30],          "fmt":_em_pct,   "hib":True},
        {"num":"31","name":"Tool Errors (top types)", "value":0.0,             "fmt":_em_count, "hib":False, "ni":True},
        {"num":"32","name":"Retrieval Precision (RAG)","value":0.0,            "fmt":_em_ratio, "hib":True,  "ni":True},
        {"num":"33","name":"MCP Server p95 Latency",  "value":0.0,             "fmt":_em_ms,    "hib":False, "ni":True},
        {"num":"34","name":"Retrieval Recall (RAG)",  "value":0.0,             "fmt":_em_ratio, "hib":True,  "ni":True},
        {"num":"35","name":"Tool Retry Rate",         "value":ev[35],          "fmt":_em_ratio, "hib":False},
        {"num":"36","name":"Invalid Tool Arg Rate",   "value":0.0,             "fmt":_em_pct,   "hib":False, "ni":True},
        {"num":"B1","name":"Tool Selection Accuracy", "value":ev["tool_sel"],  "fmt":_em_ratio, "hib":True},
        {"num":"B2","name":"Tool Argument Accuracy",  "value":ev["tool_arg"],  "fmt":_em_ratio, "hib":True},
        {"num":"B3","name":"Tool Output Handling",    "value":ev["tool_out"],  "fmt":_em_ratio, "hib":True},
    ])
    _em_section("6 — Reliability & Safety  (8 metrics)", [
        {"num":"37","name":"Agent Failure Rate",           "value":ev[37],        "fmt":_em_pct,   "hib":False},
        {"num":"38","name":"Error Recovery Rate",          "value":ev[38],        "fmt":_em_ratio, "hib":True},
        {"num":"39","name":"Toxicity Score",               "value":ev["toxicity"], "fmt":_em_ratio, "hib":True},
        {"num":"40","name":"Cascade Failure Rate",         "value":0.0,           "fmt":_em_pct,   "hib":False, "ni":True},
        {"num":"41","name":"Circuit Breaker Trigger Rate", "value":0.0,           "fmt":_em_pct,   "hib":False, "ni":True},
        {"num":"42","name":"Timeout Rate",                 "value":ev[42],        "fmt":_em_pct,   "hib":False},
        {"num":"43","name":"Prompt Injection Detection",   "value":0.0,           "fmt":_em_pct,   "hib":True,  "ni":True},
        {"num":"D1","name":"Bias Score",                   "value":ev["bias"],    "fmt":_em_ratio, "hib":True},
    ])
    _em_section("7 — Memory & Context Management  (6 metrics)", [
        {"num":"44","name":"Context Carry-over Fidelity",  "value":0.0,    "fmt":_em_ratio, "hib":True,  "ni":True},
        {"num":"45","name":"Memory Retrieval Accuracy",    "value":0.0,    "fmt":_em_ratio, "hib":True,  "ni":True},
        {"num":"46","name":"Memory Hit Rate",              "value":0.0,    "fmt":_em_pct,   "hib":True,  "ni":True},
        {"num":"47","name":"Working Memory Overflow Rate", "value":ev[47], "fmt":_em_pct,   "hib":False},
        {"num":"48","name":"Memory Staleness Rate",        "value":0.0,    "fmt":_em_pct,   "hib":False, "ni":True},
        {"num":"49","name":"Redundant Retrieval Rate",     "value":0.0,    "fmt":_em_pct,   "hib":False, "ni":True},
    ])
    _em_section("8 — Observability & Tracing  (7 metrics)", [
        {"num":"50","name":"Trace Completeness Rate",   "value":ev[50], "fmt":_em_pct,   "hib":True},
        {"num":"51","name":"Span Attribution Accuracy", "value":ev[51], "fmt":_em_pct,   "hib":True},
        {"num":"52","name":"Log-Trace Correlation Rate","value":0.0,    "fmt":_em_pct,   "hib":True,  "ni":True},
        {"num":"53","name":"Alert Precision",           "value":0.0,    "fmt":_em_ratio, "hib":True,  "ni":True},
        {"num":"54","name":"Alert Recall",              "value":0.0,    "fmt":_em_ratio, "hib":True,  "ni":True},
        {"num":"55","name":"Metric Ingestion Latency",  "value":ev[55], "fmt":_em_sec,   "hib":False},
        {"num":"56","name":"Dead Span Rate",            "value":ev[56], "fmt":_em_pct,   "hib":False},
    ])
    _em_section("9 — Conversation Quality  (4 metrics)", [
        {"num":"C1","name":"Knowledge Retention",       "value":ev["know"],  "fmt":_em_ratio, "hib":True},
        {"num":"C2","name":"Role Adherence",            "value":ev["role"],  "fmt":_em_ratio, "hib":True},
        {"num":"C3","name":"Conversation Completeness", "value":ev["comp"],  "fmt":_em_ratio, "hib":True},
        {"num":"C4","name":"Conversation Relevancy",    "value":ev["relev"], "fmt":_em_ratio, "hib":True},
    ])

    st.divider()
    st.caption("Grey (—) = needs additional instrumentation. 44–46/48–49: persistent memory store. 52: Loki log trace IDs. 53/54: Alertmanager history. A1–A4: LLM judges (QA, rubric, hallucination, conciseness). B1–B3: granular tool eval. C1–C4: multi-turn sessions. D1: bias judge.")

    # Score Analysis
    st.header("Score Analysis")
    _em_score_rows = db._execute(
        f"SELECT metric, score, trace_id, evaluator, eval_type, evaluated_at "
        f"FROM otel.eval_scores WHERE 1=1 {_em_fs()} ORDER BY evaluated_at DESC LIMIT 5000"
    )
    if not _em_score_rows:
        st.info("No eval scores match the current filters.")
    else:
        em_scores_df = pd.DataFrame(_em_score_rows)
        em_scores_df["score"] = em_scores_df["score"].astype(float)
        em_all_metrics = sorted(em_scores_df["metric"].unique().tolist())

        em_sel_metrics = st.multiselect(
            "Filter / sort by metric (leave empty for all)",
            options=em_all_metrics, key="em_metric_filter",
        )
        em_filtered_df = em_scores_df[em_scores_df["metric"].isin(em_sel_metrics)] if em_sel_metrics else em_scores_df

        em_sa1, em_sa2, em_sa3 = st.tabs(["📊 Score Overview", "📈 Score Trends", "📉 Distribution"])

        with em_sa1:
            em_agg = (
                em_filtered_df.groupby("metric")["score"]
                .agg(avg_score="mean", num_scores="count", min_score="min", max_score="max")
                .reset_index().sort_values("avg_score", ascending=False)
            )
            em_agg["avg_score"] = em_agg["avg_score"].round(4)
            fig_bar = px.bar(
                em_agg, x="avg_score", y="metric", orientation="h",
                color="avg_score", color_continuous_scale=["#d73027","#fee090","#1a9850"],
                range_color=[0,1], text="avg_score",
                custom_data=["num_scores","min_score","max_score"],
                labels={"avg_score":"Avg Score","metric":"Metric"},
                title="Average Score per Metric",
            )
            fig_bar.update_traces(
                texttemplate="%{text:.3f}", textposition="outside",
                hovertemplate="<b>%{y}</b><br>Avg: %{x:.4f}<br>n=%{customdata[0]}<br>Min: %{customdata[1]:.3f}  Max: %{customdata[2]:.3f}<extra></extra>",
            )
            fig_bar.update_layout(xaxis_range=[0,1.1], showlegend=False, height=max(300, len(em_agg)*36+80))
            st.plotly_chart(fig_bar, use_container_width=True)

            def _em_sty(val):
                try:
                    f = float(val)
                except Exception:
                    return ""
                if f >= 0.8: return "background-color:#d4edda;color:#155724"
                if f >= 0.5: return "background-color:#fff3cd;color:#856404"
                return "background-color:#f8d7da;color:#721c24"

            em_agg_d = em_agg.rename(columns={"avg_score":"avg","num_scores":"n","min_score":"min","max_score":"max"})
            st.dataframe(em_agg_d.style.applymap(_em_sty, subset=["avg"]), use_container_width=True, hide_index=True)

        with em_sa2:
            em_trend_days = st.slider("Days to show", 3, 30, 7, key="em_trend_days")
            em_trend_rows = db._execute(
                f"SELECT toDate(evaluated_at) AS day, metric, avg(score) AS avg_score, count() AS n "
                f"FROM otel.eval_scores "
                f"WHERE evaluated_at >= now() - INTERVAL {em_trend_days} DAY {_em_fs()} "
                f"{'AND metric IN (' + ','.join([chr(39)+m+chr(39) for m in em_sel_metrics]) + ')' if em_sel_metrics else ''} "
                f"GROUP BY day, metric ORDER BY day ASC"
            )
            if not em_trend_rows:
                st.info("No trend data for the selected filters and time range.")
            else:
                em_trend_df = pd.DataFrame(em_trend_rows)
                em_trend_df["avg_score"] = em_trend_df["avg_score"].astype(float)
                em_trend_df["day"]       = em_trend_df["day"].astype(str)
                fig_line = px.line(
                    em_trend_df, x="day", y="avg_score", color="metric", markers=True,
                    title=f"Daily Avg Score per Metric — last {em_trend_days} days",
                    labels={"day":"Date","avg_score":"Avg Score","metric":"Metric"},
                )
                fig_line.update_layout(yaxis_range=[0,1.05], height=450)
                st.plotly_chart(fig_line, use_container_width=True)

        with em_sa3:
            fig_hist = px.histogram(
                em_filtered_df, x="score", color="metric", nbins=20,
                barmode="overlay", opacity=0.72, title="Score Distribution",
                labels={"score":"Score","count":"Count"},
            )
            fig_hist.update_layout(height=420)
            st.plotly_chart(fig_hist, use_container_width=True)
            fig_box = px.box(
                em_filtered_df, x="metric", y="score", color="metric",
                title="Score Box Plot by Metric",
                labels={"metric":"Metric","score":"Score"},
            )
            fig_box.update_layout(showlegend=False, height=400, xaxis_tickangle=-30)
            st.plotly_chart(fig_box, use_container_width=True)

# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — THRESHOLDS
# ══════════════════════════════════════════════════════════════════════════════
with tab_thresh:
    st.subheader("🚨 Alerts and Thresholds")
    st.caption(
        "Metrics grouped by category. "
        "**White** = live, threshold-capable (click to configure). "
        "**Orange** = threshold configured. "
        "**Blue** = OTel-computed (no LLM score). "
        "**Grey dashed** = needs instrumentation."
    )

    # Ensure table exists
    try:
        db._execute_no_result("""
            CREATE TABLE IF NOT EXISTS otel.alert_thresholds (
                metric     String,
                operator   LowCardinality(String) DEFAULT 'lt',
                threshold  Float32,
                enabled    UInt8    DEFAULT 1,
                updated_at DateTime DEFAULT now()
            ) ENGINE = ReplacingMergeTree(updated_at)
            ORDER BY metric
        """)
    except Exception:
        pass

    _TH_OPERATORS  = {"lt":"< (below)","lte":"≤ (at or below)","gt":"> (above)","gte":"≥ (at or above)"}
    _TH_OP_SYMBOLS = {"lt":"<","lte":"≤","gt":">","gte":"≥"}
    _TH_META: dict = {
        "task_latency_ms":            {"default":5000.0,   "max":300000.0,"step":500.0,  "unit":"ms",     "op":"gt"},
        "agent_hop_latency_ms":       {"default":1000.0,   "max":60000.0, "step":100.0,  "unit":"ms",     "op":"gt"},
        "token_count":                {"default":50000.0,  "max":500000.0,"step":1000.0, "unit":"tokens", "op":"gt"},
        "tool_calls_per_task":        {"default":20.0,     "max":500.0,   "step":1.0,    "unit":"calls",  "op":"gt"},
        "cost_usd":                   {"default":0.05,     "max":10.0,    "step":0.005,  "unit":"USD",    "op":"gt"},
        "task_success":               {"default":0.95,"max":1.0,"step":0.05,"unit":"score","op":"lt"},
        "agent_failure":              {"default":0.10,"max":1.0,"step":0.05,"unit":"score","op":"gt"},
        "timeout":                    {"default":0.10,"max":1.0,"step":0.05,"unit":"score","op":"gt"},
        "tool_call_success_rate":     {"default":0.80,"max":1.0,"step":0.05,"unit":"score","op":"lt"},
        "handoff_success_rate":       {"default":0.80,"max":1.0,"step":0.05,"unit":"score","op":"lt"},
        "context_window_utilization": {"default":0.80,"max":1.0,"step":0.05,"unit":"score","op":"gt"},
        "dead_span_rate":             {"default":0.10,"max":1.0,"step":0.05,"unit":"score","op":"gt"},
    }

    try:
        _th_map: dict = {r["metric"]: r for r in db.get_thresholds()}
    except Exception:
        _th_map = {}

    _TH_SECTIONS = [
        ("1 — Task Completion & Accuracy", [
            {"num":"01","name":"Task Success Rate",         "eval_key":"task_success",          "ni":False},
            {"num":"02","name":"Subtask Completion Rate",   "eval_key":None,                    "ni":True},
            {"num":"03","name":"Goal Achievement Fidelity", "eval_key":"faithfulness",          "ni":False},
            {"num":"04","name":"Hallucination Rate",        "eval_key":"agent_failure",         "ni":False},
            {"num":"05","name":"Output Correctness Score",  "eval_key":"relevance",             "ni":False},
            {"num":"06","name":"Instruction Adherence",     "eval_key":"instruction_following", "ni":False},
            {"num":"07","name":"False Refusal Rate",        "eval_key":None,                    "ni":True},
            {"num":"A1","name":"Q&A Correctness",           "eval_key":"qa_correctness",        "ni":False},
            {"num":"A2","name":"Custom Rubric Score",       "eval_key":"custom_rubric",         "ni":False},
            {"num":"A3","name":"Hallucination Score",       "eval_key":"hallucination_score",   "ni":False},
            {"num":"A4","name":"Conciseness",               "eval_key":"conciseness",           "ni":False},
        ]),
        ("2 — Efficiency & Cost", [
            {"num":"08","name":"E2E Task Latency",       "eval_key":"task_latency_ms",      "ni":False},
            {"num":"09","name":"Cost per Task (USD)",     "eval_key":"cost_usd",             "ni":False},
            {"num":"10","name":"Time to First Token",    "eval_key":None,                   "ni":False},
            {"num":"11","name":"Agent Hop Latency",      "eval_key":"agent_hop_latency_ms", "ni":False},
            {"num":"12","name":"Token Usage per Task",   "eval_key":"token_count",          "ni":False},
            {"num":"13","name":"Tool Calls per Task",    "eval_key":"tool_calls_per_task",  "ni":False},
            {"num":"14","name":"Token Efficiency Ratio", "eval_key":None,                   "ni":True},
        ]),
        ("3 — Multi-Agent Coordination", [
            {"num":"15","name":"Handoff Success Rate",         "eval_key":"handoff_success_rate",        "ni":False},
            {"num":"16","name":"Delegation Accuracy",          "eval_key":"instruction_following",       "ni":False},
            {"num":"17","name":"Context Propagation Fidelity", "eval_key":"context_propagation_fidelity","ni":False},
            {"num":"18","name":"Orchestrator Plan Quality",    "eval_key":"instruction_following",       "ni":False},
            {"num":"19","name":"Conflict Resolution Rate",     "eval_key":None,                          "ni":True},
            {"num":"20","name":"Circular Dependency Rate",     "eval_key":None,                          "ni":True},
            {"num":"21","name":"Inter-Agent Comm Latency",     "eval_key":"agent_hop_latency_ms",        "ni":False},
            {"num":"22","name":"Idle Agent Time Ratio",        "eval_key":None,                          "ni":True},
        ]),
        ("4 — Reasoning & Decision Quality", [
            {"num":"23","name":"Tool Selection Precision",     "eval_key":"tool_accuracy",              "ni":False},
            {"num":"24","name":"Reasoning Chain Coherence",    "eval_key":"coherence",                  "ni":False},
            {"num":"25","name":"Re-planning Rate",             "eval_key":None,                         "ni":True},
            {"num":"26","name":"Dead-end Detection Rate",      "eval_key":None,                         "ni":True},
            {"num":"27","name":"Confidence Calibration (ECE)", "eval_key":None,                         "ni":True},
            {"num":"28","name":"Backtrack Rate",               "eval_key":None,                         "ni":True},
            {"num":"29","name":"Context Window Utilization",   "eval_key":"context_window_utilization", "ni":False},
        ]),
        ("5 — Tool & MCP Integration", [
            {"num":"30","name":"Tool Call Success Rate",  "eval_key":"tool_call_success_rate", "ni":False},
            {"num":"31","name":"Tool Errors (top types)", "eval_key":None,                    "ni":True},
            {"num":"32","name":"Retrieval Precision (RAG)","eval_key":None,                   "ni":True},
            {"num":"33","name":"MCP Server p95 Latency",  "eval_key":None,                    "ni":True},
            {"num":"34","name":"Retrieval Recall (RAG)",  "eval_key":None,                    "ni":True},
            {"num":"35","name":"Tool Retry Rate",         "eval_key":"tool_retry_rate",       "ni":False},
            {"num":"36","name":"Invalid Tool Arg Rate",   "eval_key":None,                    "ni":True},
            {"num":"B1","name":"Tool Selection Accuracy", "eval_key":"tool_selection_accuracy","ni":False},
            {"num":"B2","name":"Tool Argument Accuracy",  "eval_key":"tool_argument_accuracy", "ni":False},
            {"num":"B3","name":"Tool Output Handling",    "eval_key":"tool_output_handling",   "ni":False},
        ]),
        ("6 — Reliability & Safety", [
            {"num":"37","name":"Agent Failure Rate",           "eval_key":"agent_failure",       "ni":False},
            {"num":"38","name":"Error Recovery Rate",          "eval_key":"error_recovery_rate", "ni":False},
            {"num":"39","name":"Toxicity Score",               "eval_key":"toxicity_score",      "ni":False},
            {"num":"40","name":"Cascade Failure Rate",         "eval_key":None,                  "ni":True},
            {"num":"41","name":"Circuit Breaker Trigger Rate", "eval_key":None,                  "ni":True},
            {"num":"42","name":"Timeout Rate",                 "eval_key":"timeout",             "ni":False},
            {"num":"43","name":"Prompt Injection Detection",   "eval_key":None,                  "ni":True},
            {"num":"D1","name":"Bias Score",                   "eval_key":"bias_score",          "ni":False},
        ]),
        ("7 — Memory & Context Management", [
            {"num":"44","name":"Context Carry-over Fidelity",  "eval_key":None,                         "ni":True},
            {"num":"45","name":"Memory Retrieval Accuracy",    "eval_key":None,                         "ni":True},
            {"num":"46","name":"Memory Hit Rate",              "eval_key":None,                         "ni":True},
            {"num":"47","name":"Working Memory Overflow Rate", "eval_key":"context_window_utilization", "ni":False},
            {"num":"48","name":"Memory Staleness Rate",        "eval_key":None,                         "ni":True},
            {"num":"49","name":"Redundant Retrieval Rate",     "eval_key":None,                         "ni":True},
        ]),
        ("8 — Observability & Tracing", [
            {"num":"50","name":"Trace Completeness Rate",   "eval_key":"task_success",   "ni":False},
            {"num":"51","name":"Span Attribution Accuracy", "eval_key":None,             "ni":False},
            {"num":"52","name":"Log-Trace Correlation Rate","eval_key":None,             "ni":True},
            {"num":"53","name":"Alert Precision",           "eval_key":None,             "ni":True},
            {"num":"54","name":"Alert Recall",              "eval_key":None,             "ni":True},
            {"num":"55","name":"Metric Ingestion Latency",  "eval_key":None,             "ni":False},
            {"num":"56","name":"Dead Span Rate",            "eval_key":"dead_span_rate", "ni":False},
        ]),
        ("9 — Conversation Quality", [
            {"num":"C1","name":"Knowledge Retention",       "eval_key":"knowledge_retention",       "ni":False},
            {"num":"C2","name":"Role Adherence",            "eval_key":"role_adherence",            "ni":False},
            {"num":"C3","name":"Conversation Completeness", "eval_key":"conversation_completeness", "ni":False},
            {"num":"C4","name":"Conversation Relevancy",    "eval_key":"conversation_relevancy",    "ni":False},
        ]),
    ]

    @st.dialog("Configure Threshold")
    def _th_edit_dialog(metric_name: str, eval_key: str):
        existing = _th_map.get(eval_key, {})
        meta     = _TH_META.get(eval_key, {})
        unit     = meta.get("unit", "score")
        st.markdown(f"**Metric:** `{metric_name}`")
        st.markdown(f"**Stored as:** `{eval_key}` · **Unit:** `{unit}`")
        st.caption("A violation is flagged whenever a new eval score breaches this condition.")
        st.divider()
        default_op  = existing.get("operator") or meta.get("op", "lt")
        default_val = float(existing.get("threshold") or meta.get("default", 0.70))
        max_val     = float(meta.get("max", 1.0))
        step_val    = float(meta.get("step", 0.05))
        op  = st.selectbox(
            "Flag when score is…", list(_TH_OPERATORS.keys()),
            index=list(_TH_OPERATORS.keys()).index(default_op),
            format_func=lambda x: _TH_OPERATORS[x],
        )
        val = st.number_input(
            f"Threshold value ({unit})", min_value=0.0, max_value=max_val,
            value=min(default_val, max_val), step=step_val,
            format="%.4f" if unit in ("usd","score") else "%.1f",
        )
        enabled = st.toggle("Active — flag violations in feed", value=bool(existing.get("enabled", 1)))
        c1, c2 = st.columns([3, 2])
        if c1.button("Save threshold", type="primary", use_container_width=True):
            try:
                db.upsert_threshold(metric=eval_key, operator=op, threshold=val, enabled=1 if enabled else 0)
                st.rerun()
            except Exception as e:
                st.error(f"Save failed: {e}")
        if existing and c2.button("Remove", use_container_width=True):
            try:
                db.delete_threshold(eval_key)
                st.rerun()
            except Exception as e:
                st.error(f"Remove failed: {e}")

    def _th_tile(col, m: dict):
        num      = m["num"]
        name     = m["name"]
        eval_key = m.get("eval_key")
        ni       = m.get("ni", False)
        t        = _th_map.get(eval_key) if eval_key else None
        with col:
            if ni:
                st.html(f"""
                <div style="border:1px dashed #ccc;border-radius:8px;padding:10px 12px;
                            background:#f8f9fa;min-height:88px;margin-bottom:2px">
                  <div style="font-size:10px;color:#bbb;font-weight:600;letter-spacing:.5px">{num}</div>
                  <div style="font-size:12px;font-weight:600;color:#ccc;margin-top:4px;line-height:1.3">{name}</div>
                  <div style="margin-top:8px;font-size:10px;color:#ccc">⚙ Needs instrumentation</div>
                </div>""")
            elif eval_key is None:
                st.html(f"""
                <div style="border:1px solid #b8cfe8;border-radius:8px;padding:10px 12px;
                            background:#f0f5ff;min-height:88px;margin-bottom:2px">
                  <div style="font-size:10px;color:#6a8fb0;font-weight:600;letter-spacing:.5px">{num}</div>
                  <div style="font-size:12px;font-weight:600;color:#3a5f80;margin-top:4px;line-height:1.3">{name}</div>
                  <div style="margin-top:8px;font-size:10px;color:#7aabcc">📡 OTel computed</div>
                </div>""")
            elif t:
                op_sym      = _TH_OP_SYMBOLS.get(t["operator"], t["operator"])
                active_lbl  = "✅ Active" if t.get("enabled", 1) else "⏸ Paused"
                badge_bg    = "#e67e22" if t.get("enabled", 1) else "#aaa"
                meta        = _TH_META.get(eval_key, {})
                unit        = meta.get("unit", "score")
                tv          = t["threshold"]
                thresh_str  = (f"{tv:,.0f} ms" if unit=="ms" else
                               f"${tv:.4f}" if unit=="usd" else
                               f"{tv:,.0f} {unit}" if unit in ("count","calls","tokens") else
                               f"{tv:.3f}")
                st.html(f"""
                <div style="border:2px solid #e67e22;border-radius:8px;padding:10px 12px;
                            background:#fef6e8;min-height:88px;margin-bottom:2px">
                  <div style="font-size:10px;color:#b05010;font-weight:600;letter-spacing:.5px">{num}</div>
                  <div style="font-size:12px;font-weight:700;color:#7a3800;margin-top:4px;line-height:1.3">{name}</div>
                  <div style="margin-top:6px">
                    <span style="font-size:10px;background:{badge_bg};color:#fff;
                                 padding:2px 7px;border-radius:3px;font-weight:600;white-space:nowrap">
                      {op_sym} {thresh_str} &nbsp;·&nbsp; {active_lbl}
                    </span>
                  </div>
                </div>""")
                if st.button("Edit", key=f"th_btn_{num}", use_container_width=True):
                    _th_edit_dialog(name, eval_key)
            else:
                st.html(f"""
                <div style="border:1px solid #dee2e6;border-radius:8px;padding:10px 12px;
                            background:#fff;min-height:88px;margin-bottom:2px">
                  <div style="font-size:10px;color:#999;font-weight:600;letter-spacing:.5px">{num}</div>
                  <div style="font-size:12px;font-weight:600;color:#333;margin-top:4px;line-height:1.3">{name}</div>
                  <div style="margin-top:8px;font-size:10px;color:#bbb">No threshold set</div>
                </div>""")
                if st.button("＋ Set threshold", key=f"th_btn_{num}", use_container_width=True):
                    _th_edit_dialog(name, eval_key)

    # Summary metrics
    th_live    = sum(1 for s in _TH_SECTIONS for m in s[1] if not m["ni"])
    th_capable = sum(1 for s in _TH_SECTIONS for m in s[1] if not m["ni"] and m.get("eval_key"))
    th_set     = len(_th_map)
    th_active  = sum(1 for t in _th_map.values() if t.get("enabled", 1))

    sc1, sc2, sc3, sc4 = st.columns(4)
    sc1.metric("Live metrics",          th_live)
    sc2.metric("Threshold-capable",     th_capable)
    sc3.metric("Thresholds configured", th_set)
    sc4.metric("Active thresholds",     th_active)
    st.divider()

    for _th_sec_title, _th_metrics in _TH_SECTIONS:
        live_n = sum(1 for m in _th_metrics if not m["ni"])
        ni_n   = sum(1 for m in _th_metrics if m["ni"])
        with st.expander(f"**{_th_sec_title}**  ·  {live_n} live · {ni_n} need instrumentation", expanded=True):
            th_cols = st.columns(6)
            for i, m in enumerate(_th_metrics):
                _th_tile(th_cols[i % 6], m)

    st.divider()
    st.subheader("Violations Feed")

    try:
        _th_all   = db.get_thresholds()
        _th_activ = [r for r in _th_all if r.get("enabled", 1)]
    except Exception:
        _th_activ = []

    th_h_col, th_r_col = st.columns([2, 1])
    with th_h_col:
        th_hours = st.select_slider(
            "Look-back window", options=[1,6,12,24,48,168], value=24,
            format_func=lambda x: f"Last {x}h" if x < 168 else "Last 7d",
            key="th_hours",
        )
    with th_r_col:
        if st.button("↻ Refresh", use_container_width=True, key="th_refresh"):
            st.rerun()

    if not _th_activ:
        st.info("No active thresholds — configure one on any orange-eligible tile above.")
    else:
        try:
            violations = db.get_violations(hours=th_hours, limit=200)
        except Exception as e:
            st.error(f"Could not load violations: {e}")
            violations = []

        if not violations:
            st.success(f"No threshold violations in the last {th_hours}h — all metrics within bounds.")
        else:
            v_metrics = list({v["metric"] for v in violations})
            v_traces  = list({v["trace_id"] for v in violations})
            b1, b2, b3 = st.columns(3)
            b1.metric("Total violations", len(violations))
            b2.metric("Distinct traces",  len(v_traces))
            b3.metric("Metrics breached", len(v_metrics))
            st.markdown(f"**Metrics with violations:** {', '.join(sorted(v_metrics))}")
            st.divider()

            def _th_trunc(text, n=2000):
                s = str(text or "")
                return s[:n] + "…" if len(s) > n else s

            for v in violations:
                trace_id  = v.get("trace_id", "") or ""
                metric    = v.get("metric", "")
                score     = float(v.get("score", 0))
                reasoning = v.get("reasoning", "") or ""
                agent     = v.get("agent_name", "") or "unknown"
                prompt    = v.get("prompt_text", "") or ""
                response  = v.get("response_text", "") or ""
                ts        = str(v.get("evaluated_at", ""))[:19]
                eval_type = v.get("eval_type", "") or ""
                span_id   = v.get("span_id", "") or ""

                with st.expander(f"**{metric}** · score {score:.3f} · {agent} · {ts}", expanded=False):
                    hc1, hc2, hc3 = st.columns([3, 3, 2])
                    with hc1:
                        st.markdown(f"**Metric:** `{metric}`")
                        st.markdown(f"**Score:** {score:.4f}")
                        st.markdown(f"**Eval type:** `{eval_type}`")
                    with hc2:
                        st.markdown(f"**Agent:** `{agent}`")
                        st.markdown(f"**Trace:** `{trace_id[:20]}…`" if trace_id else "**Trace:** —")
                        if span_id:
                            st.markdown(f"**Span:** `{span_id[:16]}…`")
                    with hc3:
                        if trace_id:
                            st.markdown(f"[🔍 Open in Jaeger]({JAEGER_BASE}/trace/{trace_id})")
                        st.markdown(f"**Time:** {ts}")
                    if reasoning:
                        st.markdown("**Judge reasoning:**")
                        st.info(reasoning)
                    if prompt:
                        st.markdown("**Prompt:**")
                        st.code(_th_trunc(prompt), language=None)
                    if response:
                        st.markdown("**Response:**")
                        st.write(_th_trunc(response))
