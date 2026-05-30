"""
Context propagation fidelity evaluator.
Checks that child/downstream spans carry the task input context forward,
rather than losing it across agent hops.
Score = spans_with_input / total_agent_spans.
Covers metric #17 (context propagation fidelity).
"""

from evaluators.base import BaseEvaluator, EvalResult

# Attribute keys that represent "input context is present"
_INPUT_KEYS = ("task.input", "agent.input", "input", "langchain.inputs", "crewai.task.description")


class ContextPropagationEvaluator(BaseEvaluator):
    """Checks how many agent spans carry forward a non-empty input context."""

    name = "context_propagation"
    metric = "context_propagation_fidelity"
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
                reasoning="No agent spans found; context propagation not applicable.",
                eval_type=self.eval_type,
            )

        total = len(agent_spans)
        with_context = 0

        for s in agent_spans:
            attrs = s.get("attributes", {})
            if any(attrs.get(k) for k in _INPUT_KEYS):
                with_context += 1

        score = with_context / total
        reasoning = (
            f"{with_context}/{total} agent spans carry an input context attribute. "
            f"Fidelity: {score:.1%}."
        )

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
