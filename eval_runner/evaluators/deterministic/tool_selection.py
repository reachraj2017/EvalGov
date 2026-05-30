"""
Tool Selection evaluator.
Checks that each tool call has a valid, non-generic tool name — i.e. the agent
actually selected a specific tool rather than failing to identify one.
Complements tool_accuracy (which checks success/failure) by checking whether
the right kind of tool was invoked.
"""

import structlog

from evaluators.base import BaseEvaluator, EvalResult

log = structlog.get_logger(__name__)

# Tool names that indicate a selection failure or placeholder
_INVALID_NAMES = {"unknown", "", "none", "null", "undefined"}


class ToolSelectionEvaluator(BaseEvaluator):
    """Scores whether tool names are valid and specific (not generic/missing)."""

    name = "tool_selection"
    metric = "tool_selection_accuracy"
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
        valid = 0
        invalid_names: list[str] = []

        for ts in tool_spans:
            attrs     = ts.get("attributes", {})
            tool_name = (attrs.get("tool.name", "") or "").strip().lower()

            if tool_name and tool_name not in _INVALID_NAMES:
                valid += 1
            else:
                invalid_names.append(attrs.get("tool.name", "(empty)"))

        score = valid / total

        if invalid_names:
            reasoning = (
                f"{valid}/{total} tool calls had valid tool names. "
                f"Invalid/missing: {', '.join(invalid_names)}"
            )
        else:
            reasoning = f"All {total} tool call(s) had valid tool names"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
