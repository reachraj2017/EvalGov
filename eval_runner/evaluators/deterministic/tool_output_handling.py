"""
Tool Output Handling Evaluator

Checks whether the agent actually used tool results in its final response.
A tool call is wasted if the result is available but the task output shows
no sign of incorporating it.

Score per trace:
    1.0  = all tool outputs are reflected in the agent's task output
    0.5  = some tool outputs appear used, others ignored
    0.0  = tool outputs exist but none are reflected in the response

Heuristic: for each tool span that produced a non-empty output, check whether
any meaningful substring (≥10 chars) of that output appears in task.output.
"""

from evaluators.base import BaseEvaluator, EvalResult


class ToolOutputHandlingEvaluator(BaseEvaluator):
    name = "tool_output_handling"
    metric = "tool_output_handling"
    eval_type = "deterministic"

    _MIN_OVERLAP = 10   # characters that must appear verbatim
    _MAX_TOOL_OUT = 300 # only look at the first N chars of each tool output

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        tool_spans: list[dict] = context.get("tool_spans") or []
        attrs = span.get("attributes", {}) or {}
        task_output = (attrs.get("task.output") or attrs.get("output") or "").lower()

        if not tool_spans:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="No tool calls in this trace; metric not applicable.",
                eval_type=self.eval_type,
            )

        outputs_with_content = []
        for ts in tool_spans:
            ta = ts.get("attributes", {}) or {}
            for key in ("tool.output", "output", "tool.result"):
                val = str(ta.get(key) or "").strip()
                if val and val.lower() not in ("none", "null", ""):
                    outputs_with_content.append(val[:self._MAX_TOOL_OUT])
                    break

        if not outputs_with_content:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning="Tool calls produced no captured outputs; cannot assess handling.",
                eval_type=self.eval_type,
            )

        if not task_output:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning=f"{len(outputs_with_content)} tool output(s) available but task.output is empty.",
                eval_type=self.eval_type,
            )

        used = sum(
            1 for out in outputs_with_content
            if self._output_used(out.lower(), task_output)
        )

        score = used / len(outputs_with_content)
        reasoning = (
            f"{used}/{len(outputs_with_content)} tool output(s) reflected in agent response."
        )

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=round(score, 4),
            reasoning=reasoning,
            eval_type=self.eval_type,
        )

    def _output_used(self, tool_out: str, task_out: str) -> bool:
        """Check if a meaningful chunk of tool_out appears verbatim in task_out."""
        chunk_size = self._MIN_OVERLAP
        # Slide a window over the tool output looking for a match
        for start in range(0, len(tool_out) - chunk_size + 1, chunk_size):
            chunk = tool_out[start:start + chunk_size].strip()
            if len(chunk) >= chunk_size and chunk in task_out:
                return True
        return False
