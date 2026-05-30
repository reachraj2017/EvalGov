"""
Handoff fidelity judge.
Evaluates whether context was preserved accurately across agent handoffs.
"""

import structlog

from evaluators.base import EvalResult
from evaluators.llm_judges.judge_base import JudgeBase

log = structlog.get_logger(__name__)


class HandoffFidelityJudge(JudgeBase):
    """Scores context preservation quality across multi-agent handoffs."""

    name = "handoff_fidelity_judge"
    metric = "handoff_fidelity"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        handoff_spans: list[dict] = context.get("handoff_spans", [])

        if not handoff_spans:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No handoffs in this trace",
                eval_type=self.eval_type,
            )

        scores: list[float] = []
        reasonings: list[str] = []

        for hs in handoff_spans:
            hs_attrs = hs.get("attributes", {})
            from_agent = hs_attrs.get("from.agent_id", "unknown")
            to_agent = hs_attrs.get("to.agent_id", "unknown")
            handoff_reason = hs_attrs.get("handoff.reason", "")
            handoff_ctx = hs_attrs.get("handoff.context", "")

            # Quick deterministic checks first
            if not handoff_reason and not handoff_ctx:
                scores.append(0.0)
                reasonings.append(
                    f"Handoff {from_agent}->{to_agent}: no reason or context provided"
                )
                continue

            if not handoff_ctx:
                scores.append(0.3)
                reasonings.append(
                    f"Handoff {from_agent}->{to_agent}: reason provided but context is empty"
                )
                continue

            # LLM judge for content quality
            criteria = (
                "Rate how complete and coherent the handoff context payload is "
                "given the stated handoff reason. "
                "1.0 = context is thorough and clearly supports the stated reason. "
                "0.0 = context is missing, incoherent, or irrelevant to the reason."
            )

            prompt = self._build_prompt(
                criteria=criteria,
                input_text=f"Handoff reason: {handoff_reason}",
                output_text=f"Handoff context payload:\n{handoff_ctx[:2000]}",
                context_text=f"From agent: {from_agent} → To agent: {to_agent}",
            )

            try:
                score, reasoning = self._call_judge(prompt)
                scores.append(score)
                reasonings.append(
                    f"Handoff {from_agent}->{to_agent}: {reasoning}"
                )
            except Exception as exc:
                log.error(
                    "handoff_fidelity_judge_failed",
                    from_agent=from_agent,
                    to_agent=to_agent,
                    error=str(exc),
                )
                scores.append(0.5)
                reasonings.append(
                    f"Handoff {from_agent}->{to_agent}: judge call failed – {exc}"
                )

        avg_score = sum(scores) / len(scores) if scores else 1.0
        combined_reasoning = " | ".join(reasonings)

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=avg_score,
            reasoning=combined_reasoning,
            eval_type=self.eval_type,
        )
