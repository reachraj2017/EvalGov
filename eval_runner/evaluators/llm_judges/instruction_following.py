"""
Instruction following judge.
Evaluates whether the agent followed the instructions in the prompt.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class InstructionFollowingJudge(JudgeBase):
    """Scores how well the output obeys explicit instructions."""

    name = "instruction_following_judge"
    metric = "instruction_following"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs = span.get("attributes", {})
        task_output = attrs.get("task.output", "")

        # Pull instructions from the first LLM call's prompt
        instructions = ""
        llm_spans: list[dict] = context.get("llm_spans", [])
        if llm_spans:
            first_llm = llm_spans[0]
            llm_attrs = first_llm.get("attributes", {})
            instructions = llm_attrs.get("gen_ai.prompt.0.content", "")

        # Fall back to task.input if no LLM span available
        if not instructions:
            instructions = attrs.get("task.input", "")

        if not task_output:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning="task.output is empty; cannot evaluate instruction following",
                eval_type=self.eval_type,
            )

        if not instructions:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.5,
                reasoning=(
                    "No instructions found in LLM prompt or task.input; "
                    "instruction following cannot be evaluated"
                ),
                eval_type=self.eval_type,
            )

        criteria = (
            "Rate how well the output follows the explicit instructions given. "
            "1.0 = all instructions are followed precisely. "
            "0.0 = instructions are largely ignored or violated."
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text=instructions[:2000],
            output_text=task_output[:2000],
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("instruction_following_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
