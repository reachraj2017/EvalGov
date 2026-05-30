"""
Step efficiency evaluator.
Measures actual steps taken vs. expected minimum steps.
"""

import structlog

from evaluators.base import BaseEvaluator, EvalResult

log = structlog.get_logger(__name__)


class StepEfficiencyEvaluator(BaseEvaluator):
    """Penalises traces that use far more steps than the minimum expected."""

    name = "step_efficiency"
    metric = "step_efficiency"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        all_spans: list[dict] = context.get("all_spans", [])

        # Count LLM call spans
        llm_calls = sum(
            1
            for s in all_spans
            if "llm" in s.get("span_name", "").lower()
            or "chat" in s.get("span_name", "").lower()
        )

        # Count tool call spans
        tool_calls = sum(
            1
            for s in all_spans
            if s.get("span_name") == "agent.tool_call"
        )

        total_steps = llm_calls + tool_calls
        expected_min_steps: int = int(context.get("expected_min_steps", 1))

        score = self.clamp_score(expected_min_steps / max(total_steps, 1))

        reasoning = (
            f"Used {total_steps} step(s) (LLM calls: {llm_calls}, "
            f"tool calls: {tool_calls}); expected minimum: {expected_min_steps}. "
            f"Efficiency score: {score:.2f}"
        )

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
