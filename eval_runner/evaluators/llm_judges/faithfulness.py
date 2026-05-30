"""
Faithfulness judge.
Evaluates whether the agent output is faithful to the context/sources provided.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class FaithfulnessJudge(JudgeBase):
    """Scores how faithfully the agent output reflects its source context."""

    name = "faithfulness_judge"
    metric = "faithfulness"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs = span.get("attributes", {})
        task_output = attrs.get("task.output", "")
        task_input = attrs.get("task.input", "")

        # Use the first LLM call's prompt as the "context" the agent saw
        llm_context = ""
        llm_spans: list[dict] = context.get("llm_spans", [])
        if llm_spans:
            first_llm = llm_spans[0]
            llm_attrs = first_llm.get("attributes", {})
            llm_context = llm_attrs.get("gen_ai.prompt.0.content", "")

        if not llm_context:
            llm_context = task_input

        if not task_output:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning="task.output is empty; cannot evaluate faithfulness",
                eval_type=self.eval_type,
            )

        criteria = (
            "Rate how faithful the output is to the provided context. "
            "1.0 = fully faithful with no contradictions or unsupported claims. "
            "0.0 = directly contradicts context or contains major unsupported assertions."
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text=task_input or "(no explicit input provided)",
            output_text=task_output,
            context_text=llm_context[:3000],  # guard against huge prompts
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("faithfulness_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
