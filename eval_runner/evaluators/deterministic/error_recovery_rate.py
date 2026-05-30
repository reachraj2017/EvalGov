"""
Error recovery rate evaluator.
Looks for error spans that are followed by a successful span of the same type,
indicating the agent recovered rather than propagating the failure.
Score = recovered_errors / total_errors (1.0 if no errors).
Covers metric #38 (error recovery rate).
"""

from evaluators.base import BaseEvaluator, EvalResult


class ErrorRecoveryRateEvaluator(BaseEvaluator):
    """Measures how often errors are recovered within the same trace."""

    name = "error_recovery_rate"
    metric = "error_recovery_rate"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        all_spans: list[dict] = context.get("all_spans", [])

        # Sort by timestamp to assess temporal ordering
        sorted_spans = sorted(
            all_spans,
            key=lambda s: s.get("timestamp", ""),
        )

        error_spans = [
            s for s in sorted_spans
            if s.get("status_code") == "STATUS_CODE_ERROR"
        ]

        if not error_spans:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No errors detected in trace — full recovery score.",
                eval_type=self.eval_type,
            )

        # Build index: span_id → position in sorted list
        span_positions = {s["span_id"]: i for i, s in enumerate(sorted_spans)}

        # For each error span, check if a later span with the same name succeeded
        recovered = 0
        for err_span in error_spans:
            err_name = err_span.get("span_name", "")
            err_pos = span_positions.get(err_span["span_id"], -1)
            # Look for a later span with same name that succeeded
            for later_span in sorted_spans[err_pos + 1:]:
                if (
                    later_span.get("span_name") == err_name
                    and later_span.get("status_code") != "STATUS_CODE_ERROR"
                ):
                    recovered += 1
                    break

        total_errors = len(error_spans)
        score = recovered / total_errors

        reasoning = (
            f"{recovered}/{total_errors} errors were followed by a successful "
            f"retry of the same operation. Recovery rate: {score:.1%}."
        )

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
