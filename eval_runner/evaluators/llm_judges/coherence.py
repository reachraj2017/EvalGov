"""
Coherence & Fluency Judge

Evaluates whether the agent's response flows logically, is internally consistent,
and is written in grammatically correct, readable language.

Score: 1.0 = highly coherent, well-structured, fluent prose.
       0.0 = incoherent, contradictory, or grammatically broken.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class CoherenceJudge(JudgeBase):
    """Scores logical flow, internal consistency, and fluency of agent output."""

    name = "coherence_judge"
    metric = "coherence"
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
            "Evaluate the response on three dimensions combined into one score:\n"
            "1. **Logical flow** — ideas progress in a natural, ordered sequence; "
            "conclusions follow from premises; no non-sequiturs.\n"
            "2. **Internal consistency** — no self-contradictions within the response; "
            "claims made early are not violated later.\n"
            "3. **Fluency** — grammatically correct, readable sentences; "
            "appropriate vocabulary for the context; no garbled or truncated text.\n\n"
            "Scoring guide:\n"
            "- 1.0 = Excellent on all three dimensions\n"
            "- 0.8 = Minor issue in one dimension (e.g., one awkward sentence, slight tangent)\n"
            "- 0.5 = Moderate issue (e.g., one contradictory statement, several grammar errors)\n"
            "- 0.2 = Significant problems (e.g., ideas jump without connection, frequent errors)\n"
            "- 0.0 = Completely incoherent or unreadable"
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text=task_input or "(no explicit input provided)",
            output_text=task_output[:1500],
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("coherence_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
