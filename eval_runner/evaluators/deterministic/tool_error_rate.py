"""
Tool error rate evaluator.
Breaks down errors by tool name and scores 1.0 - error_rate.
Covers metric #31 (tool error rate by type).
"""

from collections import defaultdict

from evaluators.base import BaseEvaluator, EvalResult


class ToolErrorRateEvaluator(BaseEvaluator):
    """Scores tool calls based on per-type error breakdown."""

    name = "tool_error_rate"
    metric = "tool_error_rate"
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
        errors_by_type: dict[str, int] = defaultdict(int)
        total_by_type: dict[str, int] = defaultdict(int)

        for ts in tool_spans:
            tool_name = ts.get("attributes", {}).get("tool.name", "unknown")
            total_by_type[tool_name] += 1
            if ts.get("status_code") == "STATUS_CODE_ERROR":
                errors_by_type[tool_name] += 1
            elif ts.get("attributes", {}).get("tool.success", "true").lower() == "false":
                errors_by_type[tool_name] += 1

        total_errors = sum(errors_by_type.values())
        error_rate = total_errors / total
        score = 1.0 - error_rate

        if errors_by_type:
            breakdown = ", ".join(
                f"{t}: {errors_by_type[t]}/{total_by_type[t]}"
                for t in errors_by_type
            )
            reasoning = f"{total_errors}/{total} tool calls errored. By type: {breakdown}."
        else:
            reasoning = f"All {total} tool calls succeeded across {len(total_by_type)} tool type(s)."

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
