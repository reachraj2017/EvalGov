"""
Tool Argument Accuracy evaluator.
Checks that each tool call includes non-empty arguments (tool.input).
A tool invoked with no input is likely a misconfigured or incomplete call.
Complements tool_accuracy by checking the quality of inputs, not just outcomes.
"""

import structlog

from evaluators.base import BaseEvaluator, EvalResult

log = structlog.get_logger(__name__)


class ToolArgumentAccuracyEvaluator(BaseEvaluator):
    """Scores whether tool calls have well-formed, non-empty input arguments."""

    name = "tool_argument_accuracy"
    metric = "tool_argument_accuracy"
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

        total      = len(tool_spans)
        with_args  = 0
        missing: list[str] = []

        for ts in tool_spans:
            attrs      = ts.get("attributes", {})
            tool_name  = attrs.get("tool.name", "unknown")
            tool_input = attrs.get("tool.input", "")

            # Consider non-empty string or non-empty dict/list as having args
            has_args = bool(tool_input and str(tool_input).strip() not in ("{}", "[]", "null", "None", ""))

            if has_args:
                with_args += 1
            else:
                missing.append(tool_name)

        score = with_args / total

        if missing:
            reasoning = (
                f"{with_args}/{total} tool calls had non-empty arguments. "
                f"Tools called with no/empty args: {', '.join(missing)}"
            )
        else:
            reasoning = f"All {total} tool call(s) had non-empty arguments"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
