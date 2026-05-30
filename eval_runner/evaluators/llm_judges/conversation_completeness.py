"""
Conversation Completeness judge (multi-turn).
Evaluates whether the full conversation resolved the user's original goal.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class ConversationCompletenessJudge(JudgeBase):
    """Scores whether the conversation fully resolved the user's stated goal."""

    name = "conversation_completeness_judge"
    metric = "conversation_completeness"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        """
        context must contain 'conversation_turns': list of {user_input, agent_output}.
        """
        turns: list[dict] = context.get("conversation_turns", [])

        if not turns:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning="No conversation turns found",
                eval_type=self.eval_type,
            )

        original_goal = turns[0].get("user_input", "")

        history = "\n\n".join(
            f"Turn {i+1}:\n  User: {t.get('user_input','')[:400]}\n  Agent: {t.get('agent_output','')[:400]}"
            for i, t in enumerate(turns)
        )

        criteria = (
            f"The user's original goal was: '{original_goal[:300]}'\n\n"
            "After reading the full conversation, score whether this goal was fully resolved. "
            "1.0 = goal completely achieved — the user got a clear, complete, accurate answer. "
            "0.5 = goal partially achieved — some progress but key aspects left unresolved. "
            "0.0 = goal not achieved — agent failed, deflected, or the conversation ended without resolution."
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text=f"Original user goal: {original_goal[:500]}",
            output_text=f"Final agent response: {turns[-1].get('agent_output','')[:800]}",
            context_text=history[:4000],
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("conversation_completeness_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
