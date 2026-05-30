"""
Tool retry rate evaluator.
Detects duplicate calls to the same tool within a trace as a proxy for retries.
Score is 1.0 - retry_rate (lower retries = better).
Covers metric #35 (tool retry rate).
"""

from collections import Counter

from evaluators.base import BaseEvaluator, EvalResult


class ToolRetryRateEvaluator(BaseEvaluator):
    """Detects repeated tool calls to the same tool as implicit retries."""

    name = "tool_retry_rate"
    metric = "tool_retry_rate"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        tool_spans: list[dict] = context.get("tool_spans", [])

        if not tool_spans:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No tool calls in trace.",
                eval_type=self.eval_type,
            )

        total = len(tool_spans)

        # Check for explicit retry attribute first
        explicit_retries = sum(
            int(ts.get("attributes", {}).get("tool.retry_count", 0))
            for ts in tool_spans
        )

        # Fall back: count duplicate calls to same tool name as implicit retries
        if explicit_retries == 0:
            tool_names = [
                ts.get("attributes", {}).get("tool.name", "unknown")
                for ts in tool_spans
            ]
            counts = Counter(tool_names)
            # Retries = calls beyond the first for each tool
            implicit_retries = sum(max(0, c - 1) for c in counts.values())
            retries = implicit_retries
            method = "implicit (duplicate calls)"
        else:
            retries = explicit_retries
            method = "explicit tool.retry_count"

        retry_rate = retries / total
        score = 1.0 - retry_rate

        reasoning = (
            f"{retries} retries detected ({method}) out of {total} total tool calls "
            f"(retry rate: {retry_rate:.1%}). Score: {score:.2f}"
        )

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
