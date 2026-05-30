"""
Handoff success rate evaluator.
Scores the fraction of agent.handoff spans that completed without error.
Covers metric #15 (agent handoff success rate).
"""

from evaluators.base import BaseEvaluator, EvalResult


class HandoffSuccessRateEvaluator(BaseEvaluator):
    """Scores handoffs based on error-free completion rate."""

    name = "handoff_success_rate"
    metric = "handoff_success_rate"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        handoff_spans: list[dict] = context.get("handoff_spans", [])

        if not handoff_spans:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No agent handoffs in trace.",
                eval_type=self.eval_type,
            )

        total = len(handoff_spans)
        failed = [
            s for s in handoff_spans
            if s.get("status_code") == "STATUS_CODE_ERROR"
        ]
        successful = total - len(failed)
        score = successful / total

        if failed:
            targets = [
                s.get("attributes", {}).get("handoff.target", "unknown")
                for s in failed
            ]
            reasoning = (
                f"{successful}/{total} handoffs succeeded. "
                f"Failed targets: {', '.join(targets)}."
            )
        else:
            reasoning = f"All {total} handoff(s) completed successfully."

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
