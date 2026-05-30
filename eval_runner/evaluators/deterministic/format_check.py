"""
Format compliance evaluator.
Checks if task output meets format requirements (JSON schema, length, structure).
"""

import json
from typing import Any

import structlog

from evaluators.base import BaseEvaluator, EvalResult

log = structlog.get_logger(__name__)


class FormatComplianceEvaluator(BaseEvaluator):
    """Deterministic check that task output is present and well-formed."""

    name = "format_compliance"
    metric = "format_compliance"
    eval_type = "deterministic"

    def evaluate(self, span: dict, context: dict) -> EvalResult:
        attrs = span.get("attributes", {})
        task_output = attrs.get("task.output", "")

        # 1. Empty output → immediate failure
        if not task_output or not task_output.strip():
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=0.0,
                reasoning="task.output is empty",
                eval_type=self.eval_type,
            )

        expected_schema: Any = context.get("expected_schema")

        # 2. Schema validation path
        if expected_schema is not None:
            try:
                parsed = json.loads(task_output)
            except (json.JSONDecodeError, ValueError) as exc:
                return EvalResult(
                    evaluator=self.name,
                    metric=self.metric,
                    score=0.0,
                    reasoning=f"Output is not valid JSON: {exc}",
                    eval_type=self.eval_type,
                )

            score, reasoning = self._validate_against_schema(parsed, expected_schema)
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=score,
                reasoning=reasoning,
                eval_type=self.eval_type,
            )

        # 3. Heuristic path (no schema)
        if len(task_output.strip()) > 10:
            return EvalResult(
                evaluator=self.name,
                metric=self.metric,
                score=1.0,
                reasoning=f"Output present with {len(task_output)} characters",
                eval_type=self.eval_type,
            )

        return EvalResult(
            evaluator=self.name,
            metric=self.metric,
            score=0.5,
            reasoning=f"Output is very short ({len(task_output)} chars); may be incomplete",
            eval_type=self.eval_type,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _validate_against_schema(parsed: Any, schema: Any) -> tuple[float, str]:
        """
        Basic schema validation without jsonschema dependency.
        Supports dict-key checking and list type assertions.
        """
        if isinstance(schema, dict) and isinstance(parsed, dict):
            required_keys = schema.get("required", list(schema.keys()))
            missing = [k for k in required_keys if k not in parsed]
            if missing:
                return 0.0, f"Missing required keys: {missing}"
            return 1.0, "Output matches expected schema"

        if isinstance(schema, list) and isinstance(parsed, list):
            return 1.0, "Output is a list as expected"

        if type(parsed).__name__ == type(schema).__name__:
            return 1.0, "Output type matches expected type"

        return 0.5, (
            f"Output type {type(parsed).__name__} does not match "
            f"expected {type(schema).__name__}"
        )
