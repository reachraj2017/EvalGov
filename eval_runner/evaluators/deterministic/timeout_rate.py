"""
Timeout rate evaluator.
Detects spans that exceeded a duration threshold or carry a timeout attribute.
Score is 1.0 - timeout_rate.
Covers metric #42 (timeout rate).
"""

from evaluators.base import BaseEvaluator, EvalResult

# Default threshold: 30 seconds in nanoseconds
_DEFAULT_TIMEOUT_NS = 30_000_000_000


class TimeoutRateEvaluator(BaseEvaluator):
    """Detects timed-out agent spans by attribute or duration threshold."""

    name = "timeout_rate"
    metric = "timeout_rate"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        all_spans: list[dict] = context.get("all_spans", [])

        agent_spans = [
            s for s in all_spans
            if s.get("span_name", "").startswith("agent.")
        ]

        if not agent_spans:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No agent spans to evaluate for timeouts.",
                eval_type=self.eval_type,
            )

        total = len(agent_spans)
        timeout_count = 0
        timeout_spans: list[str] = []

        for s in agent_spans:
            attrs = s.get("attributes", {})
            # Explicit timeout flag
            if attrs.get("error.type", "").lower() in ("timeout", "timedout", "deadline_exceeded"):
                timeout_count += 1
                timeout_spans.append(s.get("span_name", "unknown"))
                continue
            if str(attrs.get("timeout", "false")).lower() == "true":
                timeout_count += 1
                timeout_spans.append(s.get("span_name", "unknown"))
                continue
            # Duration-based: spans over threshold with error status
            duration_ns = s.get("duration_ns", 0)
            if duration_ns > _DEFAULT_TIMEOUT_NS and s.get("status_code") == "STATUS_CODE_ERROR":
                timeout_count += 1
                timeout_spans.append(s.get("span_name", "unknown"))

        timeout_rate = timeout_count / total
        score = 1.0 - timeout_rate

        if timeout_count:
            reasoning = (
                f"{timeout_count}/{total} agent spans timed out "
                f"({', '.join(timeout_spans)}). Score: {score:.2f}"
            )
        else:
            reasoning = f"No timeouts detected across {total} agent span(s)."

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=score,
            reasoning=reasoning,
            eval_type=self.eval_type,
        )
