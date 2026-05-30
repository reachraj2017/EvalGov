"""
Hallucination Judge

Distinct from faithfulness: faithfulness checks whether claims are grounded
in *provided* context. This judge checks whether the output contains invented
facts — specific names, numbers, dates, URLs, or assertions — that are not
supported by *any* observable context (tool outputs, retrieved docs, llm
messages) and are stated as fact rather than opinion or estimation.

Score: 1.0 = no hallucination detected, 0.0 = clear hallucinated facts found.
"""

from .judge_base import JudgeBase


class HallucinationJudge(JudgeBase):
    name = "hallucination_judge"
    metric = "hallucination_score"
    eval_type = "llm_judge"

    def evaluate(self, span: dict, context: dict) -> "EvalResult":
        from evaluators.base import EvalResult

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

        # Build grounding context from tool outputs and LLM messages
        grounding_fragments = []

        for ts in (context.get("tool_spans") or []):
            ta = ts.get("attributes", {}) or {}
            for key in ("tool.output", "output", "tool.result"):
                val = ta.get(key, "")
                if val:
                    grounding_fragments.append(f"[tool output] {str(val)[:400]}")
                    break

        for ls in (context.get("llm_spans") or []):
            la = ls.get("attributes", {}) or {}
            for key in ("gen_ai.completion", "llm.output", "output"):
                val = la.get(key, "")
                if val:
                    grounding_fragments.append(f"[llm message] {str(val)[:400]}")
                    break

        grounding = "\n".join(grounding_fragments[:10]) if grounding_fragments else "(no grounding context captured)"

        prompt = self._build_prompt(task_input, task_output, grounding)
        score, reasoning = self._call_judge(prompt)

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )

    def _build_prompt(self, task_input: str, task_output: str, grounding: str) -> str:
        return f"""You are an expert hallucination detector for AI agent outputs.

Your task is to determine whether the agent's response contains **hallucinated facts** —
specific claims (names, numbers, dates, URLs, statistics, technical details) that:
1. Are stated as definitive facts (not hedged as estimates, opinions, or possibilities)
2. Cannot be verified from the observable grounding context below

Note: This is NOT about whether the answer is helpful or complete. Focus only on
whether concrete factual claims are invented vs. grounded.

---
USER INPUT:
{task_input[:600]}

OBSERVABLE GROUNDING CONTEXT (tool outputs, retrieved data):
{grounding}

AGENT RESPONSE:
{task_output[:800]}
---

Rate the hallucination risk on a 0.0–1.0 scale where:
- 1.0 = No hallucination: all specific factual claims are either grounded in context, appropriately hedged, or the response contains no specific factual claims
- 0.7 = Minor risk: one or two specific claims cannot be verified but are plausible
- 0.4 = Moderate: multiple unverifiable specific claims stated as fact
- 0.0 = Severe: clearly invented facts (wrong names, fabricated statistics, non-existent URLs)

Respond ONLY with valid JSON:
{{"score": <float 0.0-1.0>, "reasoning": "<one concise sentence identifying specific hallucinated claims if any, or confirming all claims are grounded>"}}"""
