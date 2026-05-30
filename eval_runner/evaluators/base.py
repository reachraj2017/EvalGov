"""Abstract base class for all evaluators."""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class EvalResult:
    """Holds the output of a single evaluator run."""

    evaluator: str
    metric: str
    score: float        # 0.0 – 1.0
    reasoning: str
    eval_type: str      # 'deterministic' | 'embedding' | 'llm_judge'

    def __post_init__(self) -> None:
        self.score = BaseEvaluator.clamp_score(self.score)


class BaseEvaluator(ABC):
    """All evaluators must subclass this."""

    name: str = ""
    metric: str = ""
    eval_type: str = "deterministic"

    @abstractmethod
    def evaluate(self, span: dict, context: dict) -> EvalResult:
        """
        Evaluate a span and return an EvalResult.

        Args:
            span:    The primary span being evaluated (normalised dict).
            context: Additional context (tool_spans, llm_spans, all_spans,
                     expected_schema, expected_min_steps, …).
        """

    @staticmethod
    def clamp_score(score: float) -> float:
        """Clamp a score to the closed interval [0.0, 1.0]."""
        return max(0.0, min(1.0, float(score)))
