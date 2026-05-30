"""
Task success rate evaluator.
Scores 1.0 if the task span completed without error, 0.0 if it failed.
Covers metric #01 (task success rate).
"""

from evaluators.base import BaseEvaluator, EvalResult


class TaskSuccessEvaluator(BaseEvaluator):
    """Binary pass/fail based on span status code."""

    name = "task_success"
    metric = "task_success_rate"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        status = span.get("status_code", "STATUS_CODE_UNSET")
        failed = status == "STATUS_CODE_ERROR"

        score = 0.0 if failed else 1.0
        reasoning = (
            f"Task span status: {status}. "
            + ("Task completed successfully." if not failed else "Task failed with error status.")
        )

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
