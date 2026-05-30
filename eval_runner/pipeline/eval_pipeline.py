"""
Eval pipeline orchestrator.
For a completed trace, runs the appropriate evaluators and saves scores.
"""

import os
import random
from pathlib import Path
from typing import Optional

import structlog
import yaml

from evaluators import EVALUATOR_MAP
from evaluators.base import EvalResult
from tracing.eval_tracer import eval_pipeline_span, eval_score_span

log = structlog.get_logger(__name__)

# Default config path relative to this file
_DEFAULT_CONFIG = Path(__file__).parent.parent / "config" / "evaluator_config.yaml"


class EvalPipeline:
    """Orchestrates the full evaluation cascade for a trace."""

    def __init__(
        self,
        repository,
        trace_assembler,
        evaluator_config_path: Optional[str] = None,
    ) -> None:
        self._repo = repository
        self._assembler = trace_assembler
        config_path = evaluator_config_path or str(_DEFAULT_CONFIG)
        self._config = self._load_config(config_path)
        self._sampling_rate: float = self._config.get("sampling", {}).get(
            "online_llm_judge_rate", 0.15
        )
        self._always_judge_on_failure: bool = self._config.get("sampling", {}).get(
            "always_judge_on_failure", True
        )
        self._input_cost_per_token:  float | None = None
        self._output_cost_per_token: float | None = None

    def _ensure_cost_rates(self) -> None:
        """Load cost-per-token rates from gov_threshold_config on first use."""
        if self._input_cost_per_token is not None:
            return
        try:
            rows = self._repo._ch.fetch_all(
                "SELECT config_key, value "
                "FROM otel.gov_threshold_config FINAL "
                "WHERE config_key IN ('budget.input_token_cost_per_1m', 'budget.output_token_cost_per_1m')"
            )
            rates = {r["config_key"]: float(r["value"]) for r in (rows or [])}
            input_per_1m  = rates.get("budget.input_token_cost_per_1m",  0.15)
            output_per_1m = rates.get("budget.output_token_cost_per_1m", 0.60)
            self._input_cost_per_token  = input_per_1m  / 1_000_000
            self._output_cost_per_token = output_per_1m / 1_000_000
            log.info("cost_rates_loaded", input_per_1m=input_per_1m, output_per_1m=output_per_1m)
        except Exception as exc:
            log.warning("cost_rates_load_failed_using_defaults", error=str(exc))
            self._input_cost_per_token  = 0.15 / 1_000_000
            self._output_cost_per_token = 0.60 / 1_000_000

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run(
        self,
        trace_id: str,
        run_id: str,
        mode: str = "online",
        hint_spans: list[dict] | None = None,
    ) -> list[EvalResult]:
        """
        Run the evaluation pipeline for a trace.

        Args:
            trace_id:   Trace to evaluate.
            run_id:     Eval run this belongs to.
            mode:       'online' (sampled LLM judges) or 'offline' (all evaluators).
            hint_spans: Spans received in the same OTLP payload. Used as a
                        fallback if ClickHouse hasn't flushed yet.

        Returns:
            List of EvalResult objects produced.
        """
        log.info("pipeline_start", trace_id=trace_id, run_id=run_id, mode=mode)

        with eval_pipeline_span(trace_id, run_id, mode):
            return self._run_inner(trace_id, run_id, mode, hint_spans)

    def _run_inner(
        self,
        trace_id: str,
        run_id: str,
        mode: str,
        hint_spans: list[dict] | None,
    ) -> list[EvalResult]:
        targets = self._assembler.extract_eval_targets(trace_id, hint_spans=hint_spans)
        task_spans = targets["task_spans"]
        tool_spans = targets["tool_spans"]
        handoff_spans = targets["handoff_spans"]
        llm_spans = targets["llm_spans"]
        all_spans = targets["all_spans"]

        results: list[EvalResult] = []
        # Per-span results: used by _save_prompt_eval so each agent row gets its own scores.
        span_results: dict[str, list[EvalResult]] = {}

        # ---- Evaluate each task span ----
        for task_span in task_spans:
            span_id = task_span.get("span_id", "")
            agent_role = task_span.get("attributes", {}).get("agent.role", "*")
            task_failed = task_span.get("status_code") == "STATUS_CODE_ERROR"
            task_input  = task_span.get("attributes", {}).get("task.input", "")

            # Look up benchmark context for Q&A correctness and rubric evaluation
            expected_output = ""
            rubric = ""
            if task_input:
                try:
                    bm = self._repo.get_benchmark_for_input(task_input)
                    if bm:
                        expected_output = bm.get("expected_output", "") or ""
                        rubric          = bm.get("rubric", "") or ""
                except Exception as _exc:
                    log.warning("benchmark_lookup_failed", error=str(_exc))

            context = {
                "tool_spans":      tool_spans,
                "handoff_spans":   handoff_spans,
                "llm_spans":       llm_spans,
                "all_spans":       all_spans,
                "expected_output": expected_output,
                "rubric":          rubric,
            }

            evaluator_names = self._get_evaluators_for_span(
                span_name="agent.task",
                agent_role=agent_role,
                mode=mode,
                task_failed=task_failed,
            )

            span_results[span_id] = []

            for ev_name in evaluator_names:
                evaluator = EVALUATOR_MAP.get(ev_name)
                if evaluator is None:
                    log.warning("evaluator_not_found", name=ev_name)
                    continue

                try:
                    with eval_score_span(ev_name, ev_name, "unknown", trace_id, run_id) as otel_span:
                        result = evaluator.evaluate(task_span, context)
                        otel_span.set_attribute("eval.metric", result.metric)
                        otel_span.set_attribute("eval.score", result.score)
                        otel_span.set_attribute("eval.eval_type", result.eval_type)
                        otel_span.set_attribute("eval.reasoning", result.reasoning[:500] if result.reasoning else "")
                    results.append(result)
                    span_results[span_id].append(result)
                    self._repo.save_score(
                        trace_id=trace_id,
                        run_id=run_id,
                        span_id=span_id,
                        evaluator=result.evaluator,
                        metric=result.metric,
                        score=result.score,
                        reasoning=result.reasoning,
                        eval_type=result.eval_type,
                    )
                    log.debug(
                        "evaluator_done",
                        evaluator=ev_name,
                        score=result.score,
                        span_id=span_id,
                    )
                except Exception as exc:
                    log.error(
                        "evaluator_failed",
                        evaluator=ev_name,
                        span_id=span_id,
                        error=str(exc),
                    )

        # ---- Evaluate tool spans ----
        tool_ev_names = self._get_evaluators_for_span(
            span_name="agent.tool_call",
            agent_role="*",
            mode=mode,
            task_failed=False,
        )
        if tool_ev_names and task_spans:
            # Attach tool scores to the first task span for simplicity
            root_task_span = task_spans[0]
            tool_context = {
                "tool_spans": tool_spans,
                "handoff_spans": handoff_spans,
                "llm_spans": llm_spans,
                "all_spans": all_spans,
            }
            for ev_name in tool_ev_names:
                evaluator = EVALUATOR_MAP.get(ev_name)
                if evaluator is None:
                    continue
                try:
                    with eval_score_span(ev_name, ev_name, "unknown", trace_id, run_id) as otel_span:
                        result = evaluator.evaluate(root_task_span, tool_context)
                        otel_span.set_attribute("eval.metric", result.metric)
                        otel_span.set_attribute("eval.score", result.score)
                        otel_span.set_attribute("eval.eval_type", result.eval_type)
                    results.append(result)
                    self._repo.save_score(
                        trace_id=trace_id,
                        run_id=run_id,
                        span_id=root_task_span.get("span_id", ""),
                        evaluator=result.evaluator,
                        metric=result.metric,
                        score=result.score,
                        reasoning=result.reasoning,
                        eval_type=result.eval_type,
                    )
                except Exception as exc:
                    log.error("tool_evaluator_failed", evaluator=ev_name, error=str(exc))

        # ---- Evaluate handoff spans ----
        handoff_ev_names = self._get_evaluators_for_span(
            span_name="agent.handoff",
            agent_role="*",
            mode=mode,
            task_failed=False,
        )
        if handoff_ev_names and task_spans:
            root_task_span = task_spans[0]
            handoff_context = {
                "tool_spans": tool_spans,
                "handoff_spans": handoff_spans,
                "llm_spans": llm_spans,
                "all_spans": all_spans,
            }
            for ev_name in handoff_ev_names:
                evaluator = EVALUATOR_MAP.get(ev_name)
                if evaluator is None:
                    continue
                try:
                    with eval_score_span(ev_name, ev_name, "unknown", trace_id, run_id) as otel_span:
                        result = evaluator.evaluate(root_task_span, handoff_context)
                        otel_span.set_attribute("eval.metric", result.metric)
                        otel_span.set_attribute("eval.score", result.score)
                        otel_span.set_attribute("eval.eval_type", result.eval_type)
                    results.append(result)
                    self._repo.save_score(
                        trace_id=trace_id,
                        run_id=run_id,
                        span_id=root_task_span.get("span_id", ""),
                        evaluator=result.evaluator,
                        metric=result.metric,
                        score=result.score,
                        reasoning=result.reasoning,
                        eval_type=result.eval_type,
                    )
                except Exception as exc:
                    log.error(
                        "handoff_evaluator_failed", evaluator=ev_name, error=str(exc)
                    )

        log.info(
            "pipeline_complete",
            trace_id=trace_id,
            run_id=run_id,
            result_count=len(results),
        )

        # ---- Write OTel-computed per-trace metrics to eval_scores ----
        # These participate in threshold alerting without needing an LLM judge.
        if task_spans:
            self._save_otel_computed_metrics(
                trace_id=trace_id,
                run_id=run_id,
                task_spans=task_spans,
                tool_spans=tool_spans,
                handoff_spans=handoff_spans,
                llm_spans=llm_spans,
                all_spans=all_spans,
            )

        # ---- Write prompt evaluation record ----
        if task_spans:
            self._save_prompt_eval(
                trace_id=trace_id,
                run_id=run_id,
                task_spans=task_spans,
                llm_spans=llm_spans,
                all_spans=all_spans,
                span_results=span_results,
            )

        return results

    # ------------------------------------------------------------------
    # OTel-computed per-trace metrics
    # ------------------------------------------------------------------

    def _save_otel_computed_metrics(
        self,
        trace_id: str,
        run_id: str,
        task_spans: list[dict],
        tool_spans: list[dict],
        handoff_spans: list[dict],
        llm_spans: list[dict],
        all_spans: list[dict],
    ) -> None:
        """
        Compute per-trace operational metrics from OTel span data and persist
        them to eval_scores with eval_type='otel_computed'.

        This makes latency, cost, token usage, success rate etc. available
        for threshold alerting alongside LLM judge scores.
        """
        # Identify root task span (parent not in our task span set)
        task_span_ids = {s["span_id"] for s in task_spans}
        root_span = next(
            (s for s in task_spans if s.get("parent_span_id", "") not in task_span_ids),
            task_spans[0],
        )
        span_id = root_span.get("span_id", "")

        metrics: dict[str, float] = {}

        # ── 1. Task success / failure ─────────────────────────────────────
        failed = root_span.get("status_code") == "STATUS_CODE_ERROR"
        metrics["task_success"]   = 0.0 if failed else 1.0
        metrics["agent_failure"]  = 1.0 if failed else 0.0

        # ── 2. Task latency ───────────────────────────────────────────────
        duration_ns = root_span.get("duration_ns", 0)
        latency_ms  = duration_ns / 1_000_000
        metrics["task_latency_ms"] = latency_ms
        # Timeout: flag if root task took > 30 seconds
        metrics["timeout"] = 1.0 if latency_ms > 30_000 else 0.0

        # ── 3. Token counts and cost ──────────────────────────────────────
        total_input = 0
        total_output = 0
        max_input_per_call = 0
        for llm in llm_spans:
            la = llm.get("attributes", {})
            try:
                inp = int(
                    la.get("gen_ai.usage.input_tokens")
                    or la.get("gen_ai.usage.prompt_tokens")
                    or 0
                )
                out = int(
                    la.get("gen_ai.usage.output_tokens")
                    or la.get("gen_ai.usage.completion_tokens")
                    or 0
                )
                total_input  += inp
                total_output += out
                max_input_per_call = max(max_input_per_call, inp)
            except (ValueError, TypeError):
                pass

        metrics["token_count"] = float(total_input + total_output)
        self._ensure_cost_rates()
        metrics["cost_usd"] = (
            total_input  * self._input_cost_per_token +
            total_output * self._output_cost_per_token
        )
        # Fraction of gpt-4o-mini 128k context used in the busiest LLM call
        metrics["context_window_utilization"] = min(max_input_per_call / 128_000.0, 1.0)

        # ── 4. Tool metrics ───────────────────────────────────────────────
        if tool_spans:
            succeeded = sum(
                1 for s in tool_spans
                if s.get("status_code", "") != "STATUS_CODE_ERROR"
            )
            metrics["tool_call_success_rate"] = succeeded / len(tool_spans)
            metrics["tool_calls_per_task"]    = float(len(tool_spans))

        # ── 5. Handoff metrics ────────────────────────────────────────────
        if handoff_spans:
            succeeded_h = sum(
                1 for s in handoff_spans
                if s.get("status_code", "") != "STATUS_CODE_ERROR"
            )
            metrics["handoff_success_rate"] = succeeded_h / len(handoff_spans)
            total_handoff_ns = sum(s.get("duration_ns", 0) for s in handoff_spans)
            metrics["agent_hop_latency_ms"] = (
                total_handoff_ns / len(handoff_spans) / 1_000_000
            )

        # ── 6. Dead span rate ─────────────────────────────────────────────
        if all_spans:
            known_ids = {s.get("span_id", "") for s in all_spans}
            orphans = sum(
                1 for s in all_spans
                if s.get("parent_span_id", "")
                and s.get("parent_span_id") not in known_ids
            )
            metrics["dead_span_rate"] = orphans / len(all_spans)

        # ── Persist ───────────────────────────────────────────────────────
        for metric_name, score in metrics.items():
            try:
                self._repo.save_score(
                    trace_id=trace_id,
                    run_id=run_id,
                    span_id=span_id,
                    evaluator="otel_computed",
                    metric=metric_name,
                    score=score,
                    reasoning="",
                    eval_type="otel_computed",
                )
            except Exception as exc:
                log.warning(
                    "otel_metric_save_failed",
                    metric=metric_name,
                    error=str(exc),
                )

    # ------------------------------------------------------------------
    # Prompt eval helper
    # ------------------------------------------------------------------

    def _save_prompt_eval(
        self,
        trace_id: str,
        run_id: str,
        task_spans: list[dict],
        llm_spans: list[dict],
        all_spans: list[dict],
        span_results: dict[str, list["EvalResult"]],
    ) -> None:
        """Write one prompt_evals row per task span so every agent is visible."""
        # Identify the root span (orchestrator): parent not in our task span set.
        task_span_ids = {s["span_id"] for s in task_spans}
        root_span = next(
            (s for s in task_spans if s.get("parent_span_id", "") not in task_span_ids),
            task_spans[0],
        )
        root_span_id = root_span.get("span_id", "")

        # Build a parent map so we can walk UP from each LLM span to find its
        # nearest owning task span. This avoids double-counting: orchestrator
        # only gets tokens for its own LLM calls, not sub-agents'.
        parent_map: dict[str, str] = {
            s.get("span_id", ""): s.get("parent_span_id", "")
            for s in all_spans
            if s.get("span_id")
        }

        def _nearest_task_ancestor(span_id: str) -> str:
            current = parent_map.get(span_id, "")
            while current:
                if current in task_span_ids:
                    return current
                current = parent_map.get(current, "")
            return root_span_id

        # Build per-task-span model map so each agent row shows its own model
        # (e.g. Qwen for searcher, BART for summarizer, NLLB for translator).
        # Uses the same _nearest_task_ancestor walk as the token map.
        span_model: dict[str, str] = {s["span_id"]: "" for s in task_spans}
        for llm in llm_spans:
            la = llm.get("attributes", {})
            m = la.get("gen_ai.request.model", la.get("llm.model", ""))
            if not m:
                continue
            owner = _nearest_task_ancestor(llm.get("span_id", ""))
            if owner in span_model and not span_model[owner]:
                span_model[owner] = m
        # Fallback for spans with no matched LLM span (e.g. orchestrator passthrough).
        _first_model = next((m for m in span_model.values() if m), "")

        # Attribute each LLM span's tokens to exactly one task span —
        # its nearest task span ancestor. Tokens never double-count.
        token_map: dict[str, list[int]] = {s["span_id"]: [0, 0] for s in task_spans}
        for llm in llm_spans:
            la = llm.get("attributes", {})
            try:
                inp = int(
                    la.get("gen_ai.usage.input_tokens")
                    or la.get("gen_ai.usage.prompt_tokens")
                    or 0
                )
                out = int(
                    la.get("gen_ai.usage.output_tokens")
                    or la.get("gen_ai.usage.completion_tokens")
                    or 0
                )
            except (ValueError, TypeError):
                inp, out = 0, 0
            owner = _nearest_task_ancestor(llm.get("span_id", ""))
            if owner in token_map:
                token_map[owner][0] += inp
                token_map[owner][1] += out

        # Total tokens across the whole trace — used for the root row so it
        # mirrors how latency_ms on the root covers the full duration.
        total_prompt_tokens = sum(t[0] for t in token_map.values())
        total_completion_tokens = sum(t[1] for t in token_map.values())

        for span in task_spans:
            attrs = span.get("attributes", {})
            span_id = span.get("span_id", "")
            is_root = span_id == root_span_id

            # Root row shows trace-total tokens (mirrors the full-duration latency_ms).
            # Sub-agent rows show only their own token slice.
            if is_root:
                span_prompt_tokens = total_prompt_tokens
                span_completion_tokens = total_completion_tokens
            else:
                span_prompt_tokens, span_completion_tokens = token_map.get(span_id, [0, 0])

            # Each span gets its own scores so sub-agent rows are fully populated.
            this_span_scores = {
                r.metric: r.score
                for r in span_results.get(span_id, [])
            }

            try:
                self._repo.save_prompt_eval(
                    trace_id=trace_id,
                    span_id=span_id,
                    run_id=run_id,
                    agent_name=attrs.get("agent.role", attrs.get("agent.id", "unknown")),
                    model=span_model.get(span_id, "") or _first_model,
                    prompt_text=attrs.get("task.input", ""),
                    response_text=attrs.get("task.output", ""),
                    latency_ms=int(span.get("duration_ns", 0) / 1_000_000),
                    prompt_tokens=span_prompt_tokens,
                    completion_tokens=span_completion_tokens,
                    scores=this_span_scores,
                )
            except Exception as exc:
                log.error(
                    "save_prompt_eval_failed",
                    trace_id=trace_id,
                    span_id=span_id,
                    error=str(exc),
                )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_evaluators_for_span(
        self,
        span_name: str,
        agent_role: str,
        mode: str,
        task_failed: bool,
    ) -> list[str]:
        """
        Resolve which evaluators should run for a given span type / role.

        In online mode, LLM judge evaluators are sampled at the configured
        rate (unless the task failed, in which case they always run).
        """
        run_llm = (
            mode == "offline"
            or (self._always_judge_on_failure and task_failed)
            or random.random() < self._sampling_rate
        )

        evaluator_names: list[str] = []
        seen: set[str] = set()

        for rule in self._config.get("evaluators", []):
            match = rule.get("match", {})
            rule_span = match.get("span_name", "*")
            rule_role = match.get("agent_role", "*")

            if rule_span != "*" and rule_span != span_name:
                continue
            if rule_role != "*" and rule_role != agent_role:
                continue

            for tier in rule.get("tiers", []):
                tier_num = tier.get("tier", 1)
                is_llm_tier = tier_num >= 4  # tiers 4+ are LLM judges

                if is_llm_tier and not run_llm:
                    continue

                for name in tier.get("run", []):
                    if name not in seen:
                        evaluator_names.append(name)
                        seen.add(name)

        return evaluator_names

    @staticmethod
    def _load_config(path: str) -> dict:
        try:
            with open(path, "r") as f:
                cfg = yaml.safe_load(f)
            log.info("evaluator_config_loaded", path=path)
            return cfg or {}
        except FileNotFoundError:
            log.warning("evaluator_config_not_found", path=path)
            return {}
        except yaml.YAMLError as exc:
            log.error("evaluator_config_parse_error", path=path, error=str(exc))
            return {}
