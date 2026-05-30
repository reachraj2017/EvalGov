"""
Role Adherence judge (multi-turn).
Evaluates whether the agent stayed consistent in its persona, tone, and
defined role across all turns of the conversation.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class RoleAdherenceJudge(JudgeBase):
    """Scores consistency of agent role/persona across conversation turns."""

    name = "role_adherence_judge"
    metric = "role_adherence"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        """
        context must contain 'conversation_turns': list of {user_input, agent_output, agent_role}.
        """
        turns: list[dict] = context.get("conversation_turns", [])

        if len(turns) < 2:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="Single-turn conversation; role adherence not applicable",
                eval_type=self.eval_type,
            )

        agent_role = turns[0].get("agent_role", "AI assistant")

        history = "\n\n".join(
            f"Turn {i+1}:\n  User: {t.get('user_input','')[:400]}\n  Agent: {t.get('agent_output','')[:400]}"
            for i, t in enumerate(turns)
        )

        criteria = (
            f"The agent's declared role is: '{agent_role}'. "
            "Score whether the agent consistently behaved within that role across ALL turns. "
            "1.0 = fully consistent — same tone, scope, and persona throughout. "
            "0.5 = mostly consistent with minor deviations. "
            "0.0 = significant role violations — acted outside scope, changed persona, or contradicted its own earlier behavior."
        )

        prompt = self._build_prompt(
            criteria=criteria,
            input_text="Multi-turn conversation (see context)",
            output_text=f"Final response (Turn {len(turns)}): {turns[-1].get('agent_output','')[:800]}",
            context_text=history[:4000],
        )

        try:
            score, reasoning = self._call_judge(prompt)
        except Exception as exc:
            log.error("role_adherence_judge_failed", error=str(exc))
            score, reasoning = 0.5, f"Judge call failed: {exc}"

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
