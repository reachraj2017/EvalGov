"""
Knowledge Retention judge (multi-turn).
Evaluates whether the agent remembered and correctly used information
stated in earlier turns of the conversation.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class KnowledgeRetentionJudge(JudgeBase):
    """Scores how well the agent retains facts across conversation turns."""

    name = "knowledge_retention_judge"
    metric = "knowledge_retention"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        """
        For multi-turn eval, span is a synthetic placeholder.
        context must contain 'conversation_turns': list of {user_input, agent_output, turn}.
        """
        turns: list[dict] = context.get("conversation_turns", [])

        if len(turns) < 2:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="Single-turn conversation; knowledge retention not applicable",
                eval_type=self.eval_type,
            )

        # Format conversation history for the judge
        history = "\n\n".join(
            f"Turn {i+1}:\n  User: {t.get('user_input','')[:500]}\n  Agent: {t.get('agent_output','')[:500]}"
            for i, t in enumerate(turns)
        )

        criteria = (
            "Review this multi-turn conversation. "
            "Score how well the agent retains and correctly applies facts, names, preferences, or "
            "constraints that were stated in earlier turns. "
            "1.0 = agent perfectly remembers and uses all earlier context. "
            "0.5 = agent partially remembers (misses some details but not critical ones). "
            "0.0 = agent ignores or contradicts facts established in earlier turns."
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text="Multi-turn conversation (see context)",
            output_text=f"Final response (Turn {len(turns)}): {turns[-1].get('agent_output','')[:1000]}",
            context_text=history[:4000],
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("knowledge_retention_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
