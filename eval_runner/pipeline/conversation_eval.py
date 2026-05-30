"""
Conversation Eval Pipeline.
Runs multi-turn evaluators over a complete conversation identified by conversation_id.
Scores are stored in eval_scores with trace_id = conversation_id and eval_type = 'multiturn_judge'.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.knowledge_retention import KnowledgeRetentionJudge
from evaluators.llm_judges.role_adherence import RoleAdherenceJudge
from evaluators.llm_judges.conversation_completeness import ConversationCompletenessJudge
from evaluators.llm_judges.conversation_relevancy import ConversationRelevancyJudge

log = structlog.get_logger(__name__)

_JUDGES = [
    KnowledgeRetentionJudge(),
    RoleAdherenceJudge(),
    ConversationCompletenessJudge(),
    ConversationRelevancyJudge(),
]


class ConversationEvalPipeline:
    """Evaluates a full multi-turn conversation once it's considered complete."""

    def __init__(self, repository) -> None:
        self._repo = repository

    def run(self, conversation_id: str, run_id: str) -> list[EvalResult]:
        """
        Fetch all turns for conversation_id and run each multi-turn judge.
        Scores are saved with trace_id=conversation_id so they appear in the
        existing eval_scores table and are queryable alongside per-trace scores.
        """
        log.info("conversation_eval_start", conversation_id=conversation_id, run_id=run_id)

        turns = self._repo.get_conversation_turns(conversation_id)

        if not turns:
            log.warning("conversation_eval_no_turns", conversation_id=conversation_id)
            return []

        if len(turns) < 2:
            log.info("conversation_eval_single_turn_skip", conversation_id=conversation_id)
            return []

        log.info("conversation_eval_turns", conversation_id=conversation_id, turn_count=len(turns))

        # Build context shared by all judges
        context = {
            "conversation_turns": [
                {
                    "turn":         i + 1,
                    "user_input":   t.get("user_input", ""),
                    "agent_output": t.get("agent_output", ""),
                    "agent_role":   t.get("agent_role", "assistant"),
                }
                for i, t in enumerate(turns)
            ]
        }

        # Synthetic span representing the whole conversation
        synthetic_span: dict = {
            "span_id":    f"conv_{conversation_id[:16]}",
            "attributes": {
                "conversation.id": conversation_id,
                "task.input":  turns[0].get("user_input", ""),
                "task.output": turns[-1].get("agent_output", ""),
            },
        }

        results: list[EvalResult] = []

        for judge in _JUDGES:
            try:
                result = judge.evaluate(synthetic_span, context)
                results.append(result)
                self._repo.save_score(
                    trace_id=conversation_id,
                    run_id=run_id,
                    span_id=f"conv_{conversation_id[:16]}",
                    evaluator=result.evaluator,
                    metric=result.metric,
                    score=result.score,
                    reasoning=result.reasoning,
                    eval_type=result.eval_type,
                )
                log.debug(
                    "conversation_judge_done",
                    judge=judge.name,
                    score=result.score,
                    conversation_id=conversation_id,
                )
            except Exception as exc:
                log.error(
                    "conversation_judge_failed",
                    judge=judge.name,
                    conversation_id=conversation_id,
                    error=str(exc),
                )

        log.info(
            "conversation_eval_complete",
            conversation_id=conversation_id,
            scores=len(results),
        )
        return results
