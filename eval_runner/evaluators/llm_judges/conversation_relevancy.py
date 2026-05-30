"""
Conversation Relevancy judge (multi-turn).

Evaluates whether each agent response stays on-topic relative to the
conversation arc — specifically, whether the agent drifts from the user's
original intent as the conversation progresses.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class ConversationRelevancyJudge(JudgeBase):
    """Scores how well each agent response stays relevant to the conversation goal."""

    name = "conversation_relevancy_judge"
    metric = "conversation_relevancy"
    eval_type = "multiturn_judge"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        turns: list[dict] = context.get("conversation_turns", [])

        if len(turns) < 2:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="Single-turn conversation; relevancy tracking not applicable.",
                eval_type=self.eval_type,
            )

        history = "\n\n".join(
            f"Turn {t['turn']}:\n  User: {t.get('user_input','')[:400]}\n  Agent: {t.get('agent_output','')[:400]}"
            for t in turns
        )
        original_intent = turns[0].get("user_input", "")

        criteria = (
            f"The user's original intent was: \"{original_intent[:300]}\"\n\n"
            "Evaluate whether EACH agent response across all turns remains relevant "
            "to this original intent and the natural progression of the conversation. "
            "Penalise: tangents unrelated to the user's goal, responses that address "
            "a different topic without explanation, and answers that ignore the "
            "conversational context. "
            "1.0 = every response is highly relevant to the conversation goal. "
            "0.5 = mostly relevant but one response drifts noticeably. "
            "0.0 = significant drift — agent repeatedly addresses unrelated topics."
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text=f"Original user intent: {original_intent[:400]}",
            output_text=f"Final response (Turn {len(turns)}): {turns[-1].get('agent_output','')[:800]}",
            context_text=history[:4000],
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("conversation_relevancy_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
