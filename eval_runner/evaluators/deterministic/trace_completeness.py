"""
Trace completeness evaluator.
Measures what fraction of spans (excluding the root) have a valid parent_span_id
that resolves to another span in the same trace.
Score = well-linked spans / (total - 1).
Covers metric #50 (trace completeness rate).
"""

from evaluators.base import BaseEvaluator, EvalResult


class TraceCompletenessEvaluator(BaseEvaluator):
    """Scores a trace based on how complete its parent-child span linkage is."""

    name = "trace_completeness"
    metric = "trace_completeness_rate"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        all_spans: list[dict] = context.get("all_spans", [])

        if len(all_spans) <= 1:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="Single-span trace — completeness not applicable.",
                eval_type=self.eval_type,
            )

        span_ids = {s["span_id"] for s in all_spans if s.get("span_id")}
        total = len(all_spans)
        root_count = 0
        linked_count = 0
        orphan_count = 0

        for s in all_spans:
            pid = s.get("parent_span_id", "")
            if not pid:
                root_count += 1  # legitimate root
            elif pid in span_ids:
                linked_count += 1
            else:
                orphan_count += 1  # parent missing from trace

        # Score: all non-root spans should be linked
        non_root = total - root_count
        if non_root == 0:
            score = 1.0
        else:
            score = linked_count / non_root

        reasoning = (
            f"{total} spans total: {root_count} root(s), "
            f"{linked_count} linked, {orphan_count} orphaned. "
            f"Completeness: {score:.1%}."
        )

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
