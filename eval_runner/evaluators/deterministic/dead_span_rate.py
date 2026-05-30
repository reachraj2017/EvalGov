"""
Dead span rate evaluator.
Counts orphaned spans — spans whose parent_span_id is set but doesn't match
any other span in the trace. Score = 1.0 - orphan_rate.
Covers metric #56 (dead span rate).
"""

from evaluators.base import BaseEvaluator, EvalResult


class DeadSpanRateEvaluator(BaseEvaluator):
    """Scores a trace based on how few orphaned/dead spans it contains."""

    name = "dead_span_rate"
    metric = "dead_span_rate"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        all_spans: list[dict] = context.get("all_spans", [])

        if not all_spans:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No spans in trace.",
                eval_type=self.eval_type,
            )

        span_ids = {s["span_id"] for s in all_spans if s.get("span_id")}
        total = len(all_spans)
        orphans: list[str] = []

        for s in all_spans:
            pid = s.get("parent_span_id", "")
            if pid and pid not in span_ids:
                orphans.append(s.get("span_name", s.get("span_id", "unknown")))

        orphan_rate = len(orphans) / total
        score = 1.0 - orphan_rate

        if orphans:
            reasoning = (
                f"{len(orphans)}/{total} spans are orphaned (parent not in trace): "
                f"{', '.join(orphans[:5])}{'…' if len(orphans) > 5 else ''}. "
                f"Score: {score:.2f}"
            )
        else:
            reasoning = f"No dead spans — all {total} span(s) are properly linked."

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
