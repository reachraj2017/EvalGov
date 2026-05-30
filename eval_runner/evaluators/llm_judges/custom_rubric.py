"""
Custom Rubric judge (G-Eval equivalent).
Evaluates task.output against a JSON rubric defined on the benchmark case.
Rubric format: {"criteria": ["criterion 1", "criterion 2", ...]}
Only runs when rubric is available in the eval context.
"""

import json
import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class CustomRubricJudge(JudgeBase):
    """Scores output against a per-benchmark rubric defined as a list of criteria."""

    name = "custom_rubric_judge"
    metric = "custom_rubric"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs       = span.get("attributes", {})
        task_input  = attrs.get("task.input", "")
        task_output = attrs.get("task.output", "")
        rubric_raw  = context.get("rubric", "")

        if not rubric_raw:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.5,
                reasoning="Skipped: no rubric defined for this benchmark case",
                eval_type=self.eval_type,
            )

        if not task_output:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning="task.output is empty; cannot evaluate against rubric",
                eval_type=self.eval_type,
            )

        # Parse rubric — expects {"criteria": [...]} or a plain string
        criteria_list: list[str] = []
        try:
            rubric_obj = json.loads(rubric_raw)
            if isinstance(rubric_obj, dict):
                criteria_list = rubric_obj.get("criteria", [])
            elif isinstance(rubric_obj, list):
                criteria_list = rubric_obj
        except (json.JSONDecodeError, TypeError):
            # Treat the raw string as a single criterion
            criteria_list = [rubric_raw]

        if not criteria_list:
            criteria_list = [rubric_raw]

        numbered = "\n".join(f"{i+1}. {c}" for i, c in enumerate(criteria_list))
        criteria = (
            f"Evaluate the output against ALL of the following criteria:\n{numbered}\n\n"
            "Score 1.0 if ALL criteria are fully met. "
            "Score proportionally lower for each criterion that is partially or not met. "
            "Score 0.0 if no criteria are met."
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text=task_input or "(no explicit input)",
            output_text=task_output,
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("custom_rubric_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
