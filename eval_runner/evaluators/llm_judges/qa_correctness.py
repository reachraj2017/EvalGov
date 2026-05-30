"""
Q&A Correctness judge.
Compares task.output against expected_output from the benchmark case.
Only runs when expected_output is available in the eval context.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class QACorrectnessJudge(JudgeBase):
    """Scores how correct the agent output is relative to an expected answer."""

    name = "qa_correctness_judge"
    metric = "qa_correctness"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs = span.get("attributes", {})
        task_input  = attrs.get("task.input", "")
        task_output = attrs.get("task.output", "")
        expected    = context.get("expected_output", "")

        if not expected:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.5,
                reasoning="Skipped: no expected_output defined for this benchmark case",
                eval_type=self.eval_type,
            )

        if not task_output:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning="task.output is empty; cannot evaluate correctness",
                eval_type=self.eval_type,
            )

        criteria = (
            "Compare the agent's actual output to the expected output. "
            "Score 1.0 if the answer is fully correct and equivalent in meaning. "
            "Score 0.5 if partially correct (right direction but missing details or has minor errors). "
            "Score 0.0 if wrong, contradictory, or completely off-topic."
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text=task_input or "(no explicit input)",
            output_text=task_output,
            context_text=f"Expected answer: {expected[:2000]}",
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("qa_correctness_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
