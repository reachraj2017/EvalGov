"""
Page: Eval Testing
Aggregates Runs, Benchmarks & Review, Scores, and Regression into one page with tabs.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone

import httpx
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

_here    = os.path.dirname(os.path.abspath(__file__))
_ui_root = os.path.abspath(os.path.join(_here, ".."))
if _ui_root not in sys.path:
    sys.path.insert(0, _ui_root)


st.set_page_config(layout="wide", page_title="Eval Testing", page_icon="🧪")

try:
    from db import db
    DB_AVAILABLE = True
except Exception as e:
    DB_AVAILABLE = False
    _db_err = str(e)

EVAL_RUNNER_URL = os.environ.get("EVAL_RUNNER_URL", "http://localhost:8000")
JAEGER_BASE     = os.environ.get("PUBLIC_JAEGER_URL", "http://localhost:16686")
SUITES          = ["unit", "integration", "collaboration", "production"]
RUN_TYPES       = ["benchmark", "production", "exploratory"]
DIFFICULTIES    = ["easy", "medium", "hard"]
VERSIONS        = ["v1", "v2", "v3", "v4", "v5"]
METRICS         = ["faithfulness", "relevance", "instruction_following",
                   "tool_accuracy", "step_efficiency", "handoff_fidelity"]
REGRESSION_THRESHOLD = 0.02

st.title("🧪 Eval Testing")

if not DB_AVAILABLE:
    st.error(f"Database connection unavailable: {_db_err}")
    st.stop()

tabs = st.tabs(["Runs", "Benchmarks & Review", "Scores", "Regression"])
tab_runs, tab_benchmarks, tab_scores, tab_regression = tabs


# =============================================================================
# TAB 1 — Runs
# =============================================================================
with tab_runs:
    # ── Session state ─────────────────────────────────────────────────────────
    for key in ("runs_active_edit", "runs_active_execute", "runs_confirm_delete"):
        if key not in st.session_state:
            st.session_state[key] = None

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _sty(val):
        try:
            f = float(val)
        except Exception:
            return ""
        if f >= 0.8: return "background-color:#c6efce;color:#276221"
        if f >= 0.5: return "background-color:#ffeb9c;color:#9c5700"
        return "background-color:#ffc7ce;color:#9c0006"

    def _run_benchmarks(run, benchmarks_by_id, on_progress=None):
        meta       = run.get("metadata") or {}
        endpoint   = meta.get("agent_endpoint", "")
        bm_ids     = [x for x in meta.get("benchmark_ids", "").split(",") if x]
        rid        = str(run.get("run_id", ""))
        run_name   = run.get("name", rid[:8])
        benchmarks = [benchmarks_by_id[bid] for bid in bm_ids if bid in benchmarks_by_id]
        results    = []
        for bm in benchmarks:
            bm_id      = str(bm.get("benchmark_id", ""))
            bm_name    = bm.get("name", "?")
            task_input = bm.get("task_input", "")
            started_at = datetime.now(timezone.utc)
            trace_id   = ""; status = "ok"; error_detail = ""
            try:
                with httpx.Client(timeout=120.0) as client:
                    resp = client.post(endpoint, json={
                        "message": task_input, "user_id": f"benchmark-{bm_id[:8]}",
                        "run_id": rid, "source": "benchmark",
                    })
                ended_at    = datetime.now(timezone.utc)
                duration_ms = int((ended_at - started_at).total_seconds() * 1000)
                if resp.status_code == 200:
                    data = resp.json(); trace_id = data.get("trace_id", "")
                    results.append({"run": run_name, "benchmark": bm_name, "status": "✅ ok",
                                    "trace_id": trace_id[:20], "response_preview": str(data.get("response",""))[:120]})
                else:
                    status = "error"; error_detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
                    ended_at = datetime.now(timezone.utc)
                    duration_ms = int((ended_at - started_at).total_seconds() * 1000)
                    results.append({"run": run_name, "benchmark": bm_name,
                                    "status": f"❌ HTTP {resp.status_code}", "trace_id": "", "response_preview": resp.text[:120]})
            except Exception as ex:
                ended_at = datetime.now(timezone.utc)
                duration_ms = int((ended_at - started_at).total_seconds() * 1000)
                status = "error"; error_detail = str(ex)[:500]
                results.append({"run": run_name, "benchmark": bm_name, "status": "❌ error",
                                 "trace_id": "", "response_preview": str(ex)[:120]})
            try:
                db.save_execution(run_id=rid, run_name=run_name, benchmark_id=bm_id,
                                  benchmark_name=bm_name, trace_id=trace_id, status=status,
                                  error_detail=error_detail, started_at=started_at,
                                  ended_at=ended_at, duration_ms=duration_ms, source="benchmark")
            except Exception:
                pass
            if on_progress:
                on_progress()
        return results

    # ── Load data ─────────────────────────────────────────────────────────────
    try:
        all_benchmarks = db.get_benchmarks()
    except Exception:
        all_benchmarks = []

    benchmark_options = {
        f"{b.get('name','')} [{b.get('suite','')} / {b.get('difficulty','')}]": str(b.get("benchmark_id",""))
        for b in all_benchmarks if b.get("active", 1)
    }
    benchmarks_by_id = {str(b.get("benchmark_id","")): b for b in all_benchmarks}

    try:
        runs = db.get_runs(limit=200)
    except Exception as e:
        st.error(f"Could not load runs: {e}")
        runs = []

    all_groups = sorted({
        (r.get("metadata") or {}).get("run_group", "")
        for r in runs if (r.get("metadata") or {}).get("run_group", "").strip()
    })

    # ── Section 1: Create New Run ─────────────────────────────────────────────
    st.header("Create New Run")
    with st.form("create_run_form", clear_on_submit=True):
        col1, col2, col3, col4, col5 = st.columns(5)
        with col1: run_name = st.text_input("Run Name", placeholder="e.g. experiment-42")
        with col2: suite = st.selectbox("Suite", options=SUITES)
        with col3: agent_version = st.text_input("Agent Version", placeholder="e.g. v1.2.3")
        with col4: run_type = st.selectbox("Run Type", options=RUN_TYPES)
        with col5: run_group = st.text_input("Run Group (optional)", placeholder="e.g. sprint-42")
        agent_endpoint = st.text_input("Agent Endpoint (optional)", placeholder="e.g. http://host.docker.internal:8080/chat")
        selected_benchmarks = st.multiselect("Benchmark Test Cases (optional)", options=list(benchmark_options.keys()))
        submitted = st.form_submit_button("Create Run", use_container_width=True)

    if submitted:
        if not run_name.strip():
            st.error("Run name cannot be empty.")
        elif not agent_version.strip():
            st.error("Agent version cannot be empty.")
        else:
            selected_ids = [benchmark_options[k] for k in selected_benchmarks]
            try:
                with httpx.Client(timeout=10.0) as client:
                    resp = client.post(f"{EVAL_RUNNER_URL}/runs", json={
                        "name": run_name.strip(), "suite": suite,
                        "agent_version": agent_version.strip(),
                        "metadata": {"agent_endpoint": agent_endpoint.strip(),
                                     "benchmark_ids": ",".join(selected_ids),
                                     "run_type": run_type, "run_group": run_group.strip()},
                    })
                if resp.status_code in (200, 201):
                    st.success(f"Run created. run_id: `{resp.json().get('run_id','—')}`")
                else:
                    raise ValueError(f"HTTP {resp.status_code}")
            except Exception:
                try:
                    new_id = db.create_run(name=run_name.strip(), suite=suite,
                                           agent_version=agent_version.strip(),
                                           agent_endpoint=agent_endpoint.strip(),
                                           benchmark_ids=selected_ids, run_type=run_type,
                                           run_group=run_group.strip())
                    st.success(f"Run created. run_id: `{new_id}`")
                except Exception as e:
                    st.error(f"Failed to create run: {e}")

    st.divider()

    # ── Section 2: Current Baseline ───────────────────────────────────────────
    st.header("Current Baseline Run")
    try:
        baseline = db.get_baseline_run()
        if baseline:
            bcol1, bcol2, bcol3, bcol4 = st.columns(4)
            bcol1.metric("Run Name",      baseline.get("name", "—"))
            bcol2.metric("Suite",         baseline.get("suite", "—"))
            bcol3.metric("Agent Version", baseline.get("agent_version", "—"))
            created = baseline.get("created_at", "")
            if isinstance(created, datetime): created = created.strftime("%Y-%m-%d %H:%M")
            bcol4.metric("Created", str(created))
            st.caption(f"Baseline run_id: `{baseline.get('run_id','—')}`")
        else:
            st.info("No baseline run set. Use ⭐ in the table below to mark a run as baseline.")
    except Exception as e:
        st.error(f"Could not load baseline: {e}")

    st.divider()

    # ── Section 3: All Runs ───────────────────────────────────────────────────
    st.header("All Runs")
    if not runs:
        st.info("No runs found. Create your first run above.")
    else:
        _C = [2.8, 1.2, 1.0, 1.3, 1.0, 0.5, 1.8, 0.5, 0.5, 0.5, 0.5]
        _H = ["Name", "Group", "Suite", "Version", "Type", "BMs", "Created", "⭐", "✏️", "🗑", "▶"]
        hrow = st.columns(_C)
        for col, label in zip(hrow, _H): col.markdown(f"**{label}**")
        st.markdown("---")

        for run in runs:
            rid     = str(run.get("run_id", ""))
            meta    = run.get("metadata") or {}
            created = run.get("created_at", "")
            if isinstance(created, datetime): created = created.strftime("%Y-%m-%d %H:%M")
            else: created = str(created)[:16]
            bm_count = len([x for x in meta.get("benchmark_ids","").split(",") if x])
            is_base  = bool(run.get("is_baseline", 0))
            has_exec = bool(meta.get("agent_endpoint","").strip()) and bm_count > 0

            cols = st.columns(_C)
            cols[0].write(f"{run.get('name','')}  ✅" if is_base else run.get("name",""))
            cols[1].write(meta.get("run_group",""))
            cols[2].write(run.get("suite",""))
            cols[3].write(run.get("agent_version",""))
            cols[4].write(meta.get("run_type",""))
            cols[5].write(str(bm_count))
            cols[6].write(created)

            if cols[7].button("⭐", key=f"runs_base_{rid}"):
                db.set_baseline(rid); st.success(f"`{run.get('name','')}` is now the baseline."); st.rerun()

            if cols[8].button("✏️", key=f"runs_edit_{rid}"):
                st.session_state.runs_active_edit    = None if st.session_state.runs_active_edit == rid else rid
                st.session_state.runs_active_execute = None
                st.session_state.runs_confirm_delete = None

            if cols[9].button("🗑", key=f"runs_del_{rid}"):
                st.session_state.runs_confirm_delete = None if st.session_state.runs_confirm_delete == rid else rid
                st.session_state.runs_active_edit    = None
                st.session_state.runs_active_execute = None

            if cols[10].button("▶", key=f"runs_exec_{rid}", disabled=not has_exec):
                st.session_state.runs_active_execute = None if st.session_state.runs_active_execute == rid else rid
                st.session_state.runs_active_edit    = None
                st.session_state.runs_confirm_delete = None

            if st.session_state.runs_confirm_delete == rid:
                with st.container(border=True):
                    st.warning(f"Delete run **{run.get('name','')}**? This cannot be undone.")
                    dc1, dc2 = st.columns(2)
                    if dc1.button("Confirm Delete", key=f"runs_confirm_del_{rid}", type="primary"):
                        db.delete_run(rid); st.session_state.runs_confirm_delete = None
                        st.success("Run deleted."); st.rerun()
                    if dc2.button("Cancel", key=f"runs_cancel_del_{rid}"):
                        st.session_state.runs_confirm_delete = None; st.rerun()

            if st.session_state.runs_active_edit == rid:
                with st.container(border=True):
                    st.markdown(f"**Editing: {run.get('name','')}**")
                    existing_bm_ids    = [x for x in meta.get("benchmark_ids","").split(",") if x]
                    existing_bm_labels = [lbl for lbl, bid in benchmark_options.items() if bid in existing_bm_ids]
                    with st.form(f"runs_edit_form_{rid}", clear_on_submit=False):
                        ec1, ec2, ec3, ec4, ec5 = st.columns(5)
                        with ec1: e_name = st.text_input("Run Name", value=run.get("name",""))
                        with ec2:
                            cur_suite = run.get("suite","unit")
                            e_suite = st.selectbox("Suite", SUITES, index=SUITES.index(cur_suite) if cur_suite in SUITES else 0)
                        with ec3: e_version = st.text_input("Agent Version", value=run.get("agent_version",""))
                        with ec4:
                            cur_type = meta.get("run_type","benchmark")
                            e_type = st.selectbox("Run Type", RUN_TYPES, index=RUN_TYPES.index(cur_type) if cur_type in RUN_TYPES else 0)
                        with ec5: e_group = st.text_input("Run Group", value=meta.get("run_group",""))
                        e_endpoint   = st.text_input("Agent Endpoint", value=meta.get("agent_endpoint",""))
                        e_benchmarks = st.multiselect("Benchmark Test Cases", options=list(benchmark_options.keys()), default=existing_bm_labels)
                        sc1, sc2 = st.columns(2)
                        save_edit   = sc1.form_submit_button("Save Changes", use_container_width=True)
                        cancel_edit = sc2.form_submit_button("Cancel", use_container_width=True)
                    if save_edit:
                        try:
                            db.update_run(run_id=rid, name=e_name.strip(), suite=e_suite,
                                          agent_version=e_version.strip(), agent_endpoint=e_endpoint.strip(),
                                          benchmark_ids=[benchmark_options[k] for k in e_benchmarks],
                                          run_type=e_type, run_group=e_group.strip())
                            st.session_state.runs_active_edit = None; st.success("Run updated."); st.rerun()
                        except Exception as ex:
                            st.error(f"Update failed: {ex}")
                    if cancel_edit:
                        st.session_state.runs_active_edit = None; st.rerun()

            if st.session_state.runs_active_execute == rid:
                exec_endpoint  = meta.get("agent_endpoint","")
                exec_bm_ids    = [x for x in meta.get("benchmark_ids","").split(",") if x]
                exec_benchmarks = [b for bid, b in benchmarks_by_id.items() if bid in exec_bm_ids]
                with st.container(border=True):
                    st.markdown(f"**Execute: {run.get('name','')}**")
                    st.caption(f"Endpoint: `{exec_endpoint}`  |  Benchmarks: {len(exec_benchmarks)}")
                    if exec_benchmarks:
                        st.dataframe(pd.DataFrame([{"name": b.get("name",""), "difficulty": b.get("difficulty",""),
                                                    "task_input": str(b.get("task_input",""))[:100]}
                                                   for b in exec_benchmarks]), use_container_width=True, hide_index=True)
                    xc1, xc2 = st.columns(2)
                    run_btn    = xc1.button("▶ Execute Run", key=f"runs_run_exec_{rid}", type="primary")
                    cancel_btn = xc2.button("Cancel", key=f"runs_cancel_exec_{rid}")
                    if cancel_btn:
                        st.session_state.runs_active_execute = None; st.rerun()
                    if run_btn:
                        if not exec_benchmarks:
                            st.warning("No benchmark test cases found.")
                        else:
                            exec_results = []
                            prog         = st.progress(0, text="Starting…")
                            status_area  = st.empty()
                            run_name_cur = run.get("name", rid[:8])
                            for i, bm in enumerate(exec_benchmarks):
                                bm_id      = str(bm.get("benchmark_id",""))
                                bm_name    = bm.get("name", f"benchmark-{i+1}")
                                task_input = bm.get("task_input","")
                                started_at = datetime.now(timezone.utc)
                                status_area.markdown(f"Running **{bm_name}** ({i+1}/{len(exec_benchmarks)})…")
                                trace_id_cur = ""; exec_status = "ok"; err_detail = ""
                                try:
                                    with httpx.Client(timeout=120.0) as client:
                                        resp = client.post(exec_endpoint, json={
                                            "message": task_input, "user_id": f"benchmark-{bm_id[:8]}",
                                            "run_id": rid, "source": "benchmark"})
                                    ended_at = datetime.now(timezone.utc)
                                    duration_ms = int((ended_at - started_at).total_seconds() * 1000)
                                    if resp.status_code == 200:
                                        data = resp.json(); trace_id_cur = data.get("trace_id","")
                                        exec_results.append({"benchmark": bm_name, "status": "✅ ok",
                                                             "trace_id": trace_id_cur[:20],
                                                             "response_preview": str(data.get("response",""))[:120]})
                                    else:
                                        exec_status = "error"; err_detail = f"HTTP {resp.status_code}: {resp.text[:200]}"
                                        exec_results.append({"benchmark": bm_name, "status": f"❌ HTTP {resp.status_code}",
                                                             "trace_id": "", "response_preview": resp.text[:120]})
                                except Exception as ex:
                                    ended_at = datetime.now(timezone.utc)
                                    duration_ms = int((ended_at - started_at).total_seconds() * 1000)
                                    exec_status = "error"; err_detail = str(ex)[:500]
                                    exec_results.append({"benchmark": bm_name, "status": "❌ error",
                                                         "trace_id": "", "response_preview": str(ex)[:120]})
                                try:
                                    db.save_execution(run_id=rid, run_name=run_name_cur, benchmark_id=bm_id,
                                                      benchmark_name=bm_name, trace_id=trace_id_cur,
                                                      status=exec_status, error_detail=err_detail,
                                                      started_at=started_at, ended_at=ended_at,
                                                      duration_ms=duration_ms, source="benchmark")
                                except Exception:
                                    pass
                                prog.progress((i+1)/len(exec_benchmarks), text=f"{i+1}/{len(exec_benchmarks)} done")
                                time.sleep(0.2)
                            status_area.empty()
                            st.success(f"Done — {len(exec_results)} benchmark(s) sent.")
                            st.dataframe(pd.DataFrame(exec_results), use_container_width=True, hide_index=True)
                            st.info("Eval scores will appear in the Scores tab within a few seconds.")

        st.divider()
        st.subheader("Trigger Offline Evaluation")
        st.caption("Re-score all traces in a run (e.g. after changing evaluator config).")
        run_options_eval = {f"{r.get('name','?')} ({str(r.get('run_id',''))[:8]}...)": str(r.get("run_id","")) for r in runs}
        eval_sel = st.selectbox("Select Run to Evaluate", options=list(run_options_eval.keys()), key="runs_offline_eval_sel")
        if st.button("Trigger Evaluation", key="runs_trigger_eval_btn", type="primary"):
            rid_eval = run_options_eval[eval_sel]
            try:
                with httpx.Client(timeout=30.0) as client:
                    resp = client.post(f"{EVAL_RUNNER_URL}/runs/{rid_eval}/evaluate")
                if resp.status_code in (200, 201, 202):
                    st.success(f"Evaluation triggered. Response: {resp.json()}")
                else:
                    st.error(f"Eval runner returned HTTP {resp.status_code}: {resp.text[:300]}")
            except httpx.ConnectError:
                st.error(f"Could not connect to eval runner at {EVAL_RUNNER_URL}.")
            except Exception as e:
                st.error(f"Error: {e}")

    st.divider()

    # ── Section 4: Run Suites ─────────────────────────────────────────────────
    st.header("Run Suites")
    st.markdown("A **Run Suite** is a group of runs sharing the same **Run Group** tag.")
    if not all_groups:
        st.info("No run groups defined yet. Add a **Run Group** tag when creating or editing a run.")
    else:
        gs1, gs2 = st.columns([4, 1])
        with gs1: suite_group = st.selectbox("Select Run Suite (group)", all_groups, key="runs_suite_group_sel")
        with gs2:
            if st.button("↻ Refresh", key="runs_suite_refresh"):  st.rerun()
        try: fresh_runs = db.get_runs(limit=200)
        except Exception: fresh_runs = runs
        suite_runs  = [r for r in fresh_runs if (r.get("metadata") or {}).get("run_group","") == suite_group]
        executable  = [r for r in suite_runs if (r.get("metadata") or {}).get("agent_endpoint","").strip()
                       and (r.get("metadata") or {}).get("benchmark_ids","").strip()]
        if not suite_runs:
            st.warning(f"No runs found for group **'{suite_group}'**.")
        else:
            st.markdown(f"**{len(suite_runs)} run(s) in suite '{suite_group}'** — {len(executable)} executable")
            st.dataframe(pd.DataFrame([{"Name": r.get("name",""), "Suite": r.get("suite",""),
                                        "Version": r.get("agent_version",""),
                                        "Type": (r.get("metadata") or {}).get("run_type",""),
                                        "BMs": len([x for x in (r.get("metadata") or {}).get("benchmark_ids","").split(",") if x]),
                                        "Executable": "✅" if r in executable else "❌"} for r in suite_runs]),
                         use_container_width=True, hide_index=True)
            if executable:
                sc1, sc2 = st.columns(2)
                exec_suite_btn = sc1.button(f"▶ Execute Suite '{suite_group}'", key="runs_exec_suite_btn", type="primary")
                compare_btn    = sc2.button("📊 Compare Scores", key="runs_compare_suite_btn")
                if exec_suite_btn:
                    total_bms = sum(len([x for x in (r.get("metadata") or {}).get("benchmark_ids","").split(",") if x]) for r in executable)
                    if total_bms == 0:
                        st.warning("No benchmark IDs found.")
                    else:
                        all_suite_results = []
                        prog_ph = st.empty(); grand_prog = prog_ph.progress(0, text=f"0/{total_bms} benchmarks done…")
                        _counter = [0]
                        def _on_bm_done():
                            _counter[0] += 1
                            grand_prog.progress(_counter[0]/total_bms, text=f"{_counter[0]}/{total_bms} benchmarks done…")
                        status_area = st.empty()
                        for run in executable:
                            status_area.markdown(f"**▶ Running: {run.get('name','')}**")
                            all_suite_results.extend(_run_benchmarks(run, benchmarks_by_id, on_progress=_on_bm_done))
                        prog_ph.empty(); status_area.empty()
                        st.success(f"Suite done — {len(all_suite_results)} benchmarks across {len(executable)} runs.")
                        st.dataframe(pd.DataFrame(all_suite_results), use_container_width=True, hide_index=True)
                if compare_btn:
                    comparison_rows = []
                    for run in suite_runs:
                        rid = str(run.get("run_id",""))
                        try: agg = db.get_scores_for_run(rid)
                        except Exception: agg = []
                        for row in agg:
                            comparison_rows.append({"run": run.get("name",""), "metric": row.get("metric",""),
                                                    "avg": round(float(row.get("avg_score",0)),4),
                                                    "n": int(row.get("num_scores",0))})
                    if not comparison_rows:
                        st.info("No scores found. Execute the suite first.")
                    else:
                        comp_df = pd.DataFrame(comparison_rows)
                        pivot   = comp_df.pivot_table(index="metric", columns="run", values="avg", aggfunc="mean").reset_index()
                        pivot   = pivot.rename_axis(None, axis=1)
                        run_cols = [c for c in pivot.columns if c != "metric"]
                        st.dataframe(pivot.style.applymap(_sty, subset=run_cols), use_container_width=True, hide_index=True)
                        fig = px.bar(comp_df, x="metric", y="avg", color="run", barmode="group",
                                     title=f"Score Comparison — Suite '{suite_group}'",
                                     labels={"avg": "Avg Score", "metric": "Metric", "run": "Run"},
                                     color_discrete_sequence=px.colors.qualitative.Set2)
                        fig.update_layout(yaxis_range=[0, 1.1], xaxis_tickangle=-30, height=450)
                        st.plotly_chart(fig, use_container_width=True)

    st.divider()

    # ── Section 5: Execution History ──────────────────────────────────────────
    st.header("Execution History")
    try:
        all_execs = db.get_all_executions(limit=500)
    except Exception as e:
        all_execs = []; st.error(f"Could not load execution history: {e}")

    if not all_execs:
        st.info("No executions recorded yet.")
    else:
        ef1, ef2, ef3 = st.columns(3)
        with ef1: exec_run_filter = st.selectbox("Filter by Run", ["(all)"] + sorted({e.get("run_name","") for e in all_execs if e.get("run_name","")}), key="runs_exec_hist_run")
        with ef2: exec_bm_filter = st.selectbox("Filter by Benchmark", ["(all)"] + sorted({e.get("benchmark_name","") for e in all_execs if e.get("benchmark_name","")}), key="runs_exec_hist_bm")
        with ef3: exec_status_filter = st.selectbox("Filter by Status", ["(all)", "ok", "error"], key="runs_exec_hist_status")
        filtered_execs = [e for e in all_execs
                          if (exec_run_filter    == "(all)" or e.get("run_name","")       == exec_run_filter)
                          and (exec_bm_filter    == "(all)" or e.get("benchmark_name","") == exec_bm_filter)
                          and (exec_status_filter == "(all)" or e.get("status","")        == exec_status_filter)]
        st.caption(f"{len(filtered_execs)} execution(s) shown")
        exec_rows = [{"Started": str(e.get("started_at",""))[:19], "Run": e.get("run_name",""),
                      "Benchmark": e.get("benchmark_name",""),
                      "Status": "✅" if e.get("status","") == "ok" else "❌",
                      "Duration (ms)": e.get("duration_ms",0), "Trace ID": str(e.get("trace_id",""))[:32]}
                     for e in filtered_execs]
        st.dataframe(pd.DataFrame(exec_rows), use_container_width=True, hide_index=True)

        trace_options = [f"{str(e.get('started_at',''))[:19]}  ·  {e.get('run_name','')}  ·  {e.get('benchmark_name','')}  ·  {str(e.get('trace_id',''))[:20]}"
                         for e in filtered_execs if e.get("trace_id","")]
        if trace_options:
            st.markdown("**Inspect a trace:**")
            sel_exec = st.selectbox("Select execution", trace_options, key="runs_exec_hist_trace_sel")
            sel_idx  = trace_options.index(sel_exec)
            sel_e    = [e for e in filtered_execs if e.get("trace_id","")][sel_idx]
            sel_tid  = str(sel_e.get("trace_id",""))
            if sel_tid:
                tc1, tc2 = st.columns(2)
                tc1.markdown(f"**Trace:** `{sel_tid}`")
                tc2.markdown(f"[Open in Jaeger]({JAEGER_BASE}/trace/{sel_tid})")
                det_tab1, det_tab2 = st.tabs(["🔍 Spans", "📊 Eval Scores"])
                with det_tab1:
                    try:
                        spans = db.get_trace_spans(sel_tid)
                        if not spans: st.info("No spans found.")
                        else:
                            st.dataframe(pd.DataFrame([{"SpanName": s.get("SpanName",""), "agent": (s.get("SpanAttributes",{}) or {}).get("agent.role",""),
                                                        "SpanId": str(s.get("SpanId",""))[:12]+"…", "duration_ms": f"{float(s.get('Duration',0))/1e6:.1f}",
                                                        "status": s.get("StatusCode",""), "input": str((s.get("SpanAttributes",{}) or {}).get("task.input",""))[:80]}
                                                       for s in spans]), use_container_width=True, hide_index=True)
                    except Exception as ex: st.error(f"Could not load spans: {ex}")
                with det_tab2:
                    try:
                        scores = db.get_scores_for_trace(sel_tid)
                        if not scores: st.info("No eval scores yet.")
                        else:
                            def _sty_score(val):
                                try:
                                    f = float(val)
                                except Exception: return ""
                                if f >= 0.8: return "background-color:#c6efce;color:#276221"
                                if f >= 0.5: return "background-color:#ffeb9c;color:#9c5700"
                                return "background-color:#ffc7ce;color:#9c0006"
                            sc_df = pd.DataFrame([{"metric": s.get("metric",""), "score": round(float(s.get("score",0)),4),
                                                   "eval_type": s.get("eval_type",""), "evaluator": s.get("evaluator",""),
                                                   "reasoning": str(s.get("reasoning",""))[:200]} for s in scores])
                            st.dataframe(sc_df.style.applymap(_sty_score, subset=["score"]), use_container_width=True, hide_index=True)
                    except Exception as ex: st.error(f"Could not load scores: {ex}")

    st.divider()

    # ── Section 6: Run Drill-Down ─────────────────────────────────────────────
    st.header("Run Drill-Down")
    if runs:
        drill_options = {f"{r.get('name','?')} ({str(r.get('run_id',''))[:8]}...)": str(r.get("run_id","")) for r in runs}
        drill_label = st.selectbox("Select Run", options=["— pick one —"] + list(drill_options.keys()), key="runs_drill_run_sel")
        if drill_label != "— pick one —":
            drill_run_id = drill_options[drill_label]
            drill_run    = next((r for r in runs if str(r.get("run_id","")) == drill_run_id), {})
            drill_meta   = drill_run.get("metadata") or {}
            dc1, dc2, dc3, dc4, dc5 = st.columns(5)
            dc1.metric("Name",          drill_run.get("name","—"))
            dc2.metric("Agent Version", drill_run.get("agent_version","—"))
            dc3.metric("Suite",         drill_run.get("suite","—"))
            dc4.metric("Run Type",      drill_meta.get("run_type","—"))
            dc5.metric("Run Group",     drill_meta.get("run_group","—"))
            dd_tab1, dd_tab2, dd_tab3 = st.tabs(["📊 Eval Scores", "🔍 Traces", "📝 Prompts"])
            with dd_tab1:
                try:
                    agg   = db.get_scores_for_run(drill_run_id)
                    all_s = db.get_all_scores_for_run(drill_run_id)
                except Exception as e:
                    agg, all_s = [], []; st.error(f"Could not load scores: {e}")
                if not agg: st.info("No scores yet for this run.")
                else:
                    agg_df = pd.DataFrame(agg); agg_df["avg_score"] = agg_df["avg_score"].astype(float).round(4)
                    fig = px.bar(agg_df, x="avg_score", y="metric", orientation="h", color="avg_score",
                                 color_continuous_scale=["#d73027","#fee090","#1a9850"], range_color=[0,1],
                                 text="avg_score", title="Average Score per Metric")
                    fig.update_traces(texttemplate="%{text:.3f}", textposition="outside")
                    fig.update_layout(xaxis_range=[0,1.05], showlegend=False, height=350)
                    st.plotly_chart(fig, use_container_width=True)
                    if all_s:
                        all_df = pd.DataFrame([{"trace_id": str(s.get("trace_id",""))[:14]+"…",
                                                "metric": s.get("metric",""), "score": round(float(s.get("score",0)),4),
                                                "eval_type": s.get("eval_type",""), "evaluator": s.get("evaluator",""),
                                                "reasoning": str(s.get("reasoning",""))[:150]} for s in all_s])
                        st.dataframe(all_df.style.applymap(_sty, subset=["score"]), use_container_width=True, hide_index=True)
            with dd_tab2:
                try:
                    traces = db.get_traces_for_run(drill_run_id)
                except Exception as e:
                    traces = []; st.error(f"Could not load traces: {e}")
                if not traces: st.info("No traces found for this run.")
                else:
                    t_rows = [{"trace_id": str(t.get("TraceId",""))[:20],
                               "agent.role": (t.get("SpanAttributes",{}) or {}).get("agent.role",""),
                               "task.input": str((t.get("SpanAttributes",{}) or {}).get("task.input",""))[:100],
                               "duration_s": f"{float(t.get('Duration',0))/1e9:.2f}",
                               "status": t.get("StatusCode",""), "timestamp": str(t.get("Timestamp",""))[:19]}
                              for t in traces]
                    st.dataframe(pd.DataFrame(t_rows), use_container_width=True, hide_index=True)
            with dd_tab3:
                try:
                    prompts = db.get_prompts_for_run(drill_run_id)
                except Exception as e:
                    prompts = []; st.error(f"Could not load prompts: {e}")
                if not prompts: st.info("No prompt evaluations found.")
                else:
                    p_rows = []
                    for p in prompts:
                        sc = p.get("scores") or {}; p_tok = p.get("prompt_tokens",0) or 0; c_tok = p.get("completion_tokens",0) or 0
                        row = {"created_at": str(p.get("created_at",""))[:19], "agent": p.get("agent_name",""),
                               "model": p.get("model",""), "latency_ms": p.get("latency_ms",0),
                               "total_tokens": p_tok+c_tok, "prompt": str(p.get("prompt_text",""))[:100],
                               "response": str(p.get("response_text",""))[:120], "trace_id": str(p.get("trace_id",""))[:20]}
                        for k, val in sc.items(): row[k] = round(val,3) if val is not None else None
                        p_rows.append(row)
                    p_df = pd.DataFrame(p_rows)
                    score_cols = [c for c in p_df.columns if c not in ["created_at","agent","model","latency_ms","total_tokens","prompt","response","trace_id"]]
                    st.dataframe((p_df.style.applymap(_sty, subset=score_cols) if score_cols else p_df), use_container_width=True, hide_index=True, height=350)


# =============================================================================
# TAB 2 — Benchmarks & Review
# =============================================================================
with tab_benchmarks:
    for key in ("bm_active_edit", "bm_confirm_delete"):
        if key not in st.session_state:
            st.session_state[key] = None

    @st.cache_data(ttl=30)
    def _load_benchmarks(suite):
        try: return db.get_benchmarks(suite=suite)
        except Exception: return []

    @st.cache_data(ttl=60)
    def _load_review_stats():
        try:
            total_rows    = db._execute("SELECT count() AS cnt FROM otel.eval_scores WHERE reasoning != ''")
            reviewed_rows = db._execute("SELECT count() AS cnt FROM otel.human_reviews")
            return int(total_rows[0]["cnt"] if total_rows else 0), int(reviewed_rows[0]["cnt"] if reviewed_rows else 0)
        except Exception: return 0, 0

    @st.cache_data(ttl=60)
    def _load_pending_reviews(limit=50):
        try: return db.get_pending_reviews()[:limit]
        except Exception: return []

    def _trunc(text, n=80):
        s = str(text or ""); return s[:n] + "…" if len(s) > n else s

    def _tags_str(tags):
        return ", ".join(tags) if isinstance(tags, (list, tuple)) else str(tags or "")

    def _validate_rubric(rubric_str):
        if not rubric_str.strip(): return rubric_str, None
        try: json.loads(rubric_str); return rubric_str, None
        except json.JSONDecodeError as e: return "", str(e)

    sub_bm, sub_review = st.tabs(["Benchmark Test Cases", "Human Review Queue"])

    with sub_bm:
        fc1, fc2, fc3 = st.columns([2, 2, 6])
        with fc1: suite_filter   = st.selectbox("Filter by Suite",    ["All"] + SUITES,   key="bm_suite_filter")
        with fc2: version_filter = st.selectbox("Dataset Version",    ["All"] + VERSIONS, key="bm_version_filter")
        selected_suite   = None if suite_filter   == "All" else suite_filter
        selected_version = None if version_filter == "All" else version_filter
        benchmarks_list  = _load_benchmarks(selected_suite)
        if selected_version:
            benchmarks_list = [b for b in benchmarks_list if b.get("dataset_version","v1") == selected_version]

        if not benchmarks_list:
            st.info("No benchmarks found. Add one using the form below.")
        else:
            _C2 = [2.0, 0.8, 0.7, 0.8, 1.2, 3.2, 0.5, 0.45, 0.45]
            _H2 = ["Name", "Suite", "Version", "Difficulty", "Tags", "Task Input", "Active", "✏️", "🗑"]
            hrow2 = st.columns(_C2)
            for col, label in zip(hrow2, _H2): col.markdown(f"**{label}**")
            st.markdown("---")
            for bm in benchmarks_list:
                bid  = str(bm.get("benchmark_id",""))
                tags = _tags_str(bm.get("tags",[]))
                cols = st.columns(_C2)
                cols[0].write(bm.get("name",""));        cols[1].write(bm.get("suite",""))
                cols[2].write(bm.get("dataset_version","v1")); cols[3].write(bm.get("difficulty",""))
                cols[4].write(_trunc(tags, 40));          cols[5].write(_trunc(str(bm.get("task_input","")),100))
                cols[6].write("✅" if bm.get("active",0) else "❌")
                if cols[7].button("✏️", key=f"bm_edit_{bid}"):
                    st.session_state.bm_active_edit    = None if st.session_state.bm_active_edit == bid else bid
                    st.session_state.bm_confirm_delete = None
                if cols[8].button("🗑", key=f"bm_del_{bid}"):
                    st.session_state.bm_confirm_delete = None if st.session_state.bm_confirm_delete == bid else bid
                    st.session_state.bm_active_edit    = None

                if st.session_state.bm_confirm_delete == bid:
                    with st.container(border=True):
                        st.warning(f"Mark **{bm.get('name','')}** as inactive?")
                        dc1, dc2 = st.columns(2)
                        if dc1.button("Confirm Delete", key=f"bm_confirm_del_{bid}", type="primary"):
                            db.delete_benchmark(bid); st.session_state.bm_confirm_delete = None
                            st.cache_data.clear(); st.success("Benchmark deactivated."); st.rerun()
                        if dc2.button("Cancel", key=f"bm_cancel_del_{bid}"):
                            st.session_state.bm_confirm_delete = None; st.rerun()

                if st.session_state.bm_active_edit == bid:
                    existing_tags = _tags_str(bm.get("tags",[]))
                    with st.container(border=True):
                        st.markdown(f"**Editing: {bm.get('name','')}**")
                        with st.form(f"bm_edit_form_{bid}", clear_on_submit=False):
                            ef1, ef2, ef3, ef4, ef5 = st.columns(5)
                            with ef1: e_name  = st.text_input("Name",            value=bm.get("name",""))
                            with ef2:
                                s_idx  = SUITES.index(bm.get("suite","unit")) if bm.get("suite") in SUITES else 0
                                e_suite = st.selectbox("Suite", SUITES, index=s_idx)
                            with ef3:
                                d_idx  = DIFFICULTIES.index(bm.get("difficulty","easy")) if bm.get("difficulty") in DIFFICULTIES else 0
                                e_diff = st.selectbox("Difficulty", DIFFICULTIES, index=d_idx)
                            with ef4:
                                cur_ver = bm.get("dataset_version","v1"); v_idx = VERSIONS.index(cur_ver) if cur_ver in VERSIONS else 0
                                e_ver = st.selectbox("Dataset Version", VERSIONS, index=v_idx)
                            with ef5: e_tags = st.text_input("Tags (comma-separated)", value=existing_tags)
                            e_task     = st.text_area("Task Input",       value=bm.get("task_input",""),      height=110)
                            e_expected = st.text_area("Expected Output",  value=bm.get("expected_output",""), height=70)
                            e_rubric   = st.text_area("Rubric (JSON)",    value=bm.get("rubric",""),           height=90)
                            sc1, sc2 = st.columns(2)
                            save_bm   = sc1.form_submit_button("Save Changes", use_container_width=True)
                            cancel_bm = sc2.form_submit_button("Cancel",       use_container_width=True)
                        if save_bm:
                            if not e_name.strip(): st.error("Name is required.")
                            else:
                                rubric_str, rubric_err = _validate_rubric(e_rubric)
                                if rubric_err: st.error(f"Rubric is not valid JSON: {rubric_err}")
                                else:
                                    try:
                                        db.update_benchmark(benchmark_id=bid, suite=e_suite, name=e_name.strip(),
                                                            task_input=e_task.strip(), expected_output=e_expected.strip(),
                                                            rubric=rubric_str, difficulty=e_diff,
                                                            tags=[t.strip() for t in e_tags.split(",") if t.strip()],
                                                            dataset_version=e_ver)
                                        st.session_state.bm_active_edit = None; st.cache_data.clear()
                                        st.success("Benchmark updated."); st.rerun()
                                    except Exception as ex: st.error(f"Update failed: {ex}")
                        if cancel_bm:
                            st.session_state.bm_active_edit = None; st.rerun()

        st.divider()
        st.subheader("Add Benchmark")
        with st.form("add_benchmark_form", clear_on_submit=True):
            af1, af2, af3, af4, af5 = st.columns(5)
            with af1: bm_name = st.text_input("Name", placeholder="e.g. Multi-step research task")
            with af2: bm_suite = st.selectbox("Suite", SUITES, key="bm_form_suite")
            with af3: bm_difficulty = st.selectbox("Difficulty", DIFFICULTIES, key="bm_form_diff")
            with af4: bm_version = st.selectbox("Dataset Version", VERSIONS, key="bm_form_version")
            with af5: bm_tags = st.text_input("Tags (comma-separated)")
            bm_task_input = st.text_area("Task Input", placeholder="Describe the task…", height=110)
            bc1, bc2 = st.columns(2)
            with bc1: bm_expected = st.text_area("Expected Output (optional)", height=80)
            with bc2: bm_rubric   = st.text_area("Rubric (JSON)", placeholder='{"criteria": [...]}', height=80)
            save_bm_new = st.form_submit_button("Save Benchmark", use_container_width=True)
        if save_bm_new:
            if not bm_name.strip(): st.error("Benchmark name is required.")
            elif not bm_task_input.strip(): st.error("Task input is required.")
            else:
                rubric_str, rubric_err = _validate_rubric(bm_rubric)
                if rubric_err: st.error(f"Rubric is not valid JSON: {rubric_err}")
                else:
                    try:
                        new_id = db.save_benchmark(suite=bm_suite, name=bm_name.strip(),
                                                   task_input=bm_task_input.strip(), expected_output=bm_expected.strip(),
                                                   rubric=rubric_str, difficulty=bm_difficulty,
                                                   tags=[t.strip() for t in bm_tags.split(",") if t.strip()],
                                                   dataset_version=bm_version)
                        st.cache_data.clear(); st.success(f"Benchmark saved. benchmark_id: `{new_id}`"); st.rerun()
                    except Exception as e: st.error(f"Failed to save benchmark: {e}")

    with sub_review:
        st.subheader("Human Review Queue")
        if "review_queue_loaded" not in st.session_state:
            st.session_state.review_queue_loaded = False
        if not st.session_state.review_queue_loaded:
            if st.button("Load Review Queue", type="primary"):
                st.session_state.review_queue_loaded = True; st.cache_data.clear(); st.rerun()
            st.caption("Click to load pending reviews. Queries are cached for 60 seconds.")
        else:
            pending                         = _load_pending_reviews(limit=50)
            total_scoreable, reviewed_count = _load_review_stats()
            pending_count                   = max(0, total_scoreable - reviewed_count)
            if st.button("↻ Refresh", key="review_refresh"):
                st.cache_data.clear(); st.rerun()
            mcol1, mcol2, mcol3 = st.columns(3)
            mcol1.metric("Pending Reviews", pending_count)
            mcol2.metric("Reviewed",        reviewed_count)
            mcol3.metric("Total Scoreable", total_scoreable)
            st.divider()
            if not pending:
                st.success("All scores have been reviewed. Nothing pending.")
            else:
                st.markdown(f"**{len(pending)} score(s) awaiting human review:**")
                for idx, item in enumerate(pending):
                    score_id = str(item.get("score_id",""))
                    trace_id = str(item.get("trace_id",""))
                    metric   = item.get("metric","")
                    auto_score = float(item.get("auto_score",0))
                    reasoning  = str(item.get("reasoning",""))
                    header = f"trace `{trace_id[:12]}...` | metric: **{metric}** | auto score: **{auto_score:.3f}**"
                    with st.expander(header, expanded=False):
                        st.markdown(f"**Trace ID:** `{trace_id}`")
                        st.markdown(f"**Metric:** {metric}")
                        st.markdown(f"**Auto Score:** {auto_score:.4f}")
                        st.markdown("**Evaluator Reasoning:**")
                        st.text(reasoning[:600] + ("..." if len(reasoning) > 600 else ""))
                        with st.form(f"review_form_{idx}_{score_id[:8]}", clear_on_submit=True):
                            human_score   = st.slider("Human Score", 0.0, 1.0, float(round(auto_score,2)), 0.05, key=f"hs_{idx}")
                            reviewer_name = st.text_input("Your Name / Reviewer ID", key=f"rv_{idx}")
                            review_notes  = st.text_area("Notes", key=f"rn_{idx}", height=80)
                            submit_review = st.form_submit_button("Submit Review", use_container_width=True)
                        if submit_review:
                            if not reviewer_name.strip(): st.error("Please enter your reviewer name.")
                            else:
                                try:
                                    db.save_human_review(score_id=score_id, trace_id=trace_id, metric=metric,
                                                         human_score=human_score, auto_score=auto_score,
                                                         notes=review_notes.strip(), reviewer=reviewer_name.strip())
                                    st.success(f"Review submitted for trace `{trace_id[:12]}...` / {metric}.")
                                    st.cache_data.clear(); st.rerun()
                                except Exception as e: st.error(f"Failed to save review: {e}")


# =============================================================================
# TAB 3 — Scores
# =============================================================================
with tab_scores:
    def _truncate(text, n=200):
        s = str(text) if text is not None else ""; return s[:n] + "..." if len(s) > n else s

    st.header("Select Run")
    try:
        runs_sc = db.get_runs(limit=200)
    except Exception as e:
        st.error(f"Could not load runs: {e}"); runs_sc = []

    if not runs_sc:
        st.info("No runs found. Create a run in the Runs tab first.")
    else:
        type_filter = st.selectbox("Filter runs by type", ["all","benchmark","production","exploratory"], key="scores_type_filter")
        filtered_runs_sc = [r for r in runs_sc if type_filter == "all" or (r.get("metadata") or {}).get("run_type","") == type_filter]
        run_labels_sc = {f"{r.get('name','?')} ({str(r.get('run_id',''))[:8]}...) [{r.get('suite','')}]": str(r.get("run_id",""))
                         for r in (filtered_runs_sc or runs_sc)}
        selected_label_sc  = st.selectbox("Run", options=list(run_labels_sc.keys()), key="scores_run_sel")
        selected_run_id_sc = run_labels_sc[selected_label_sc]

        try:
            agg_scores = db.get_scores_for_run(selected_run_id_sc)
            all_scores = db.get_all_scores_for_run(selected_run_id_sc)
        except Exception as e:
            st.error(f"Could not load scores: {e}"); agg_scores, all_scores = [], []

        st.divider()
        st.header("Score Overview by Metric")
        if not agg_scores:
            st.info("No scores found for this run.")
        else:
            agg_df = pd.DataFrame(agg_scores); agg_df["avg_score"] = agg_df["avg_score"].astype(float).round(4)
            fig_bar = px.bar(agg_df, x="avg_score", y="metric", orientation="h", color="avg_score",
                             color_continuous_scale=["#d73027","#fee090","#1a9850"], range_color=[0,1],
                             text="avg_score", labels={"avg_score":"Average Score","metric":"Metric"},
                             title="Average Score per Metric")
            fig_bar.update_traces(texttemplate="%{text:.3f}", textposition="outside")
            fig_bar.update_layout(xaxis_range=[0,1.05], showlegend=False, height=400)
            st.plotly_chart(fig_bar, use_container_width=True)

        st.divider()
        st.header("Score Distribution")
        if not all_scores:
            st.info("No individual scores found for this run.")
        else:
            all_df = pd.DataFrame(all_scores); all_df["score"] = all_df["score"].astype(float)
            metric_filter = st.multiselect("Filter by metric (leave empty for all)",
                                           options=sorted(all_df["metric"].unique().tolist()), key="dist_metric_filter")
            hist_df = all_df[all_df["metric"].isin(metric_filter)] if metric_filter else all_df
            fig_hist = px.histogram(hist_df, x="score", color="metric", nbins=20, barmode="overlay", opacity=0.75,
                                    title="Distribution of Scores", labels={"score":"Score","count":"Count"})
            fig_hist.update_layout(height=400); st.plotly_chart(fig_hist, use_container_width=True)

        st.divider()
        st.header("Score Trends (Last 7 Days)")
        trend_rows = []
        for metric in METRICS:
            try:
                trend_data = db.get_score_trends(metric, days=7)
                for row in trend_data:
                    trend_rows.append({"day": str(row.get("day","")), "avg_score": float(row.get("avg_score",0)), "metric": metric})
            except Exception: pass
        if trend_rows:
            trend_df = pd.DataFrame(trend_rows)
            fig_trend = px.line(trend_df, x="day", y="avg_score", color="metric", markers=True,
                                title="Daily Average Score per Metric", labels={"day":"Date","avg_score":"Avg Score","metric":"Metric"})
            fig_trend.update_layout(yaxis_range=[0,1.05], height=450); st.plotly_chart(fig_trend, use_container_width=True)
        else:
            st.info("No score trend data available for the last 7 days.")

        st.divider()
        st.header("Individual Scores")
        if not all_scores:
            st.info("No scores to display.")
        else:
            table_rows = [{"trace_id": str(s.get("trace_id",""))[:12]+"...", "metric": s.get("metric",""),
                           "score": round(float(s.get("score",0)),4), "eval_type": s.get("eval_type",""),
                           "evaluator": s.get("evaluator",""), "reasoning": _truncate(str(s.get("reasoning","")),200),
                           "evaluated_at": str(s.get("evaluated_at",""))} for s in all_scores]
            score_table_df = pd.DataFrame(table_rows)

            def _style_score(val):
                try:
                    f = float(val)
                except (TypeError, ValueError): return ""
                if f >= 0.8: return "background-color:#c6efce;color:#276221"
                elif f >= 0.5: return "background-color:#ffeb9c;color:#9c5700"
                return "background-color:#ffc7ce;color:#9c0006"

            eval_type_opts = sorted(score_table_df["eval_type"].dropna().unique().tolist())
            selected_eval_types = st.multiselect("Filter by eval_type", options=eval_type_opts, default=[], key="scores_eval_type_filter")
            if selected_eval_types:
                score_table_df = score_table_df[score_table_df["eval_type"].isin(selected_eval_types)]

            def _style_eval_type(val):
                if val == "multiturn_judge": return "background-color:#cce5ff;color:#004085;font-weight:bold"
                return ""

            styled_table = score_table_df.style.applymap(_style_score, subset=["score"]).applymap(_style_eval_type, subset=["eval_type"])
            st.dataframe(styled_table, use_container_width=True, hide_index=True)
            st.caption(f"{len(score_table_df)} score record(s) shown.")


# =============================================================================
# TAB 4 — Regression
# =============================================================================
with tab_regression:
    st.markdown("Compare any two eval runs side by side and catch score regressions before they reach production.")

    try:
        runs_reg  = db.get_runs(limit=200)
        baseline_reg = db.get_baseline_run()
    except Exception as e:
        st.error(f"Could not load runs: {e}"); runs_reg = []; baseline_reg = None

    if not runs_reg:
        st.info("No runs found. Create a run in the Runs tab first.")
    elif not baseline_reg:
        st.warning("No baseline run is set. Go to the **Runs** tab and mark a run as the baseline before using regression analysis.")
    else:
        run_labels_reg = {f"{r.get('name','?')} ({str(r.get('run_id',''))[:8]}...) [{r.get('suite','')}]": str(r.get("run_id",""))
                          for r in runs_reg}
        baseline_label_reg = next((lbl for lbl, rid in run_labels_reg.items()
                                   if rid == str(baseline_reg.get("run_id",""))), list(run_labels_reg.keys())[0])

        st.header("Select Runs to Compare")
        col1, col2 = st.columns(2)
        with col1:
            current_label = st.selectbox("Current Run (to evaluate)", options=list(run_labels_reg.keys()), index=0, key="reg_current_run_sel")
        with col2:
            compare_label = st.selectbox("Compare Against (baseline)", options=list(run_labels_reg.keys()),
                                         index=list(run_labels_reg.keys()).index(baseline_label_reg), key="reg_compare_run_sel")

        current_run_id_reg  = run_labels_reg[current_label]
        baseline_run_id_reg = run_labels_reg[compare_label]

        st.divider()
        compare_clicked = st.button("Compare Runs", type="primary", key="reg_compare_btn")
        regression_data: list[dict] = []

        if compare_clicked:
            with st.spinner("Fetching regression data…"):
                try:
                    regression_data = db.get_regression(current_run_id_reg, baseline_run_id_reg)
                except Exception as e:
                    st.error(f"Error fetching regression data: {e}")

        if compare_clicked and regression_data:
            st.divider()
            st.header("Metric Summary")
            num_cols = min(len(regression_data), 4)
            cols     = st.columns(num_cols or 1)
            for i, row in enumerate(regression_data):
                metric        = row.get("metric","?")
                current_score = float(row.get("current_score") or 0)
                baseline_score= float(row.get("baseline_score") or 0)
                delta         = float(row.get("delta") or 0)
                with cols[i % num_cols]:
                    st.metric(label=metric, value=f"{current_score:.3f}", delta=f"{delta:+.3f}", delta_color="normal")

            st.divider()
            st.header("Detailed Regression Table")
            table_rows_reg = []
            for row in regression_data:
                delta = float(row.get("delta") or 0)
                status = "Regressed 🔴" if delta <= -REGRESSION_THRESHOLD else ("Improved 🟢" if delta >= REGRESSION_THRESHOLD else "Unchanged ⚪")
                table_rows_reg.append({"metric": row.get("metric",""),
                                       "baseline_score": round(float(row.get("baseline_score") or 0),4),
                                       "current_score":  round(float(row.get("current_score")  or 0),4),
                                       "delta": round(delta,4), "status": status})

            reg_df = pd.DataFrame(table_rows_reg)

            def _style_delta(val):
                try: f = float(val)
                except (TypeError, ValueError): return ""
                if f <= -REGRESSION_THRESHOLD: return "background-color:#ffc7ce;color:#9c0006"
                elif f >= REGRESSION_THRESHOLD: return "background-color:#c6efce;color:#276221"
                return ""

            st.dataframe(reg_df.style.applymap(_style_delta, subset=["delta"]), use_container_width=True, hide_index=True)

            st.divider()
            st.header("Radar Chart: Current vs Baseline")
            metrics_list   = [r.get("metric","") for r in regression_data]
            current_scores = [float(r.get("current_score")  or 0) for r in regression_data]
            baseline_scores= [float(r.get("baseline_score") or 0) for r in regression_data]
            metrics_closed  = metrics_list   + [metrics_list[0]]
            current_closed  = current_scores + [current_scores[0]]
            baseline_closed = baseline_scores+ [baseline_scores[0]]
            fig_radar = go.Figure()
            fig_radar.add_trace(go.Scatterpolar(r=baseline_closed, theta=metrics_closed, fill="toself",
                                                name="Baseline", line_color="steelblue", opacity=0.5))
            fig_radar.add_trace(go.Scatterpolar(r=current_closed,  theta=metrics_closed, fill="toself",
                                                name="Current Run", line_color="tomato", opacity=0.5))
            fig_radar.update_layout(polar=dict(radialaxis=dict(visible=True, range=[0,1])),
                                    showlegend=True, title="Score Comparison: Current vs Baseline", height=520)
            st.plotly_chart(fig_radar, use_container_width=True)

        elif compare_clicked and not regression_data:
            st.info("No regression data found. Make sure both runs have evaluation scores.")
