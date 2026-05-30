"""
Conciseness Judge

Evaluates whether the agent's response delivers the necessary information without
unnecessary padding, repetition, excessive caveats, or off-topic content.

Score: 1.0 = precisely the right amount of content, nothing wasted.
       0.0 = severely bloated, repetitive, or padded with irrelevant content.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class ConcisenessJudge(JudgeBase):
    """Scores whether the agent answered without unnecessary padding or repetition."""

    name = "conciseness_judge"
    metric = "conciseness"
    eval_type = "llm_judge"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs = span.get("attributes", {}) or {}
        task_output = attrs.get("task.output", attrs.get("output", ""))
        task_input  = attrs.get("task.input",  attrs.get("input", ""))

        if not task_output or not task_output.strip():
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No output to evaluate.",
                eval_type=self.eval_type,
            )

        criteria = (
            "Evaluate whether the response is appropriately concise for the task. "
            "Penalise for:\n"
            "- **Padding** — filler phrases like 'Great question!', 'Certainly!', 'As an AI...'\n"
            "- **Repetition** — restating the same point in different words multiple times\n"
            "- **Over-qualification** — excessive caveats or disclaimers beyond what the task warrants\n"
            "- **Off-topic content** — tangential information not asked for\n"
            "- **Verbosity** — using 100 words where 20 would suffice\n\n"
            "Do NOT penalise for length that is genuinely necessary to answer the task fully.\n\n"
            "Scoring guide:\n"
            "- 1.0 = Perfectly concise; every sentence adds value\n"
            "- 0.8 = Slightly verbose or one unnecessary caveat, but mostly tight\n"
            "- 0.5 = Moderate padding or repetition (e.g., 20-30% of content is filler)\n"
            "- 0.2 = Significantly bloated; core answer is buried in filler\n"
            "- 0.0 = Almost entirely padding or repetition with minimal useful content"
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text=task_input or "(no explicit input provided)",
            output_text=task_output[:1500],
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("conciseness_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
