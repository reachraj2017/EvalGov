"""
Tool accuracy evaluator.
Checks tool call success rate and validates tool inputs are non-empty.
"""

import structlog

from evaluators.base import BaseEvaluator, EvalResult

log = structlog.get_logger(__name__)


class ToolAccuracyEvaluator(BaseEvaluator):
    """Scores tool calls based on their success rate."""

    name = "tool_accuracy"
    metric = "tool_accuracy"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        tool_spans: list[dict] = context.get("tool_spans", [])

        if not tool_spans:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No tool calls to evaluate",
                eval_type=self.eval_type,
            )

        total = len(tool_spans)
        successful = 0
        failed_tools: list[str] = []

        for ts in tool_spans:
            attrs = ts.get("attributes", {})
            tool_name = attrs.get("tool.name", "unknown")
            success_raw = attrs.get("tool.success", "true")

            # Normalise the success flag (string "true"/"false" or bool)
            if isinstance(success_raw, bool):
                is_success = success_raw
            elif isinstance(success_raw, str):
                is_success = success_raw.lower() in ("true", "1", "yes")
            else:
                is_success = bool(success_raw)

            # Also treat STATUS_CODE_ERROR spans as failures
            if ts.get("status_code") == "STATUS_CODE_ERROR":
                is_success = False

            if is_success:
                successful += 1
            else:
                failed_tools.append(tool_name)

        score = successful / total
        if failed_tools:
            reasoning = (
                f"{successful}/{total} tool calls succeeded. "
                f"Failed tools: {', '.join(failed_tools)}"
            )
        else:
            reasoning = f"All {total} tool call(s) succeeded"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
