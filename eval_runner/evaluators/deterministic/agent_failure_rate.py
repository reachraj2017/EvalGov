"""
Agent failure rate evaluator.
Measures the fraction of all agent spans in the trace that errored.
Score is 1.0 - failure_rate (higher = fewer failures).
Covers metric #37 (agent failure rate).
"""

from evaluators.base import BaseEvaluator, EvalResult


class AgentFailureRateEvaluator(BaseEvaluator):
    """Scores based on fraction of non-error agent spans in the trace."""

    name = "agent_failure_rate"
    metric = "agent_failure_rate"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        all_spans: list[dict] = context.get("all_spans", [])

        agent_spans = [
            s for s in all_spans
            if s.get("span_name", "").startswith("agent.")
        ]

        if not agent_spans:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No agent spans found in trace.",
                eval_type=self.eval_type,
            )

        total = len(agent_spans)
        errors = sum(1 for s in agent_spans if s.get("status_code") == "STATUS_CODE_ERROR")
        failure_rate = errors / total
        score = 1.0 - failure_rate

        reasoning = (
            f"{errors}/{total} agent spans failed "
            f"(failure rate: {failure_rate:.1%}). "
            f"Score: {score:.2f}"
        )

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
