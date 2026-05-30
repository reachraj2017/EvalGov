"""Policy evaluation engine — PASS / WARN / BLOCK."""

from dataclasses import dataclass
from enum import Enum


class Decision(Enum):
    PASS  = "pass"
    WARN  = "warn"
    BLOCK = "block"


@dataclass
class PolicyResult:
    metric:    str
    decision:  Decision
    value:     float
    threshold: float
    message:   str


_DEFAULT_POLICIES: list[tuple[str, float, bool, Decision]] = [
    ("pii_leak_rate",             0.0,  True,  Decision.WARN),
    ("pii_leak_rate",             0.10, True,  Decision.BLOCK),
    ("pii_input_rate",            0.0,  True,  Decision.WARN),
    ("prompt_snapshot_coverage",  1.0,  False, Decision.WARN),
    ("prompt_snapshot_coverage",  0.50, False, Decision.BLOCK),
    ("budget_utilization",        0.80, True,  Decision.WARN),
    ("budget_utilization",        1.00, True,  Decision.BLOCK),
]


def evaluate_policies(
    metrics: dict[str, float],
    policies: list[tuple[str, float, bool, Decision]] | None = None,
) -> list[PolicyResult]:
    applied = policies if policies is not None else _DEFAULT_POLICIES
    results: list[PolicyResult] = []
    for metric, threshold, gt_violates, decision_on_violation in applied:
        value = metrics.get(metric)
        if value is None:
            continue
        violated  = (value > threshold) if gt_violates else (value < threshold)
        direction = ">" if gt_violates else "<"
        if violated:
            msg = f"{metric} = {value:.4f}  {direction}  threshold {threshold:.4f}  →  {decision_on_violation.value.upper()}"
            results.append(PolicyResult(metric=metric, decision=decision_on_violation,
                                        value=float(value), threshold=float(threshold), message=msg))
        else:
            msg = f"{metric} = {value:.4f}  OK  (threshold {direction} {threshold:.4f})"
            results.append(PolicyResult(metric=metric, decision=Decision.PASS,
                                        value=float(value), threshold=float(threshold), message=msg))
    return results
