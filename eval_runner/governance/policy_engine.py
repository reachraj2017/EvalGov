"""Policy evaluation engine.

Applies configured gate rules to a dict of governance metrics and returns
a list of PolicyResult objects. Each result carries a decision of PASS,
WARN, or BLOCK along with the value, threshold, and a human-readable message.

Default policies implement the gate metrics from the governance framework:
  - pii_leak_rate        > 0.0   → WARN   (any PII in outputs)
  - pii_leak_rate        > 0.10  → BLOCK  (>10% of spans leaking PII)
  - pii_input_rate       > 0.0   → WARN   (PII arriving in inputs)
  - prompt_snapshot_coverage < 1.0  → WARN  (not all spans snapshotted)
  - prompt_snapshot_coverage < 0.50 → BLOCK (<50% snapshot coverage)
"""

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


# Each tuple: (metric_name, threshold, gt_means_violated, decision_if_violated)
# When gt_means_violated=True  → violated when value > threshold
# When gt_means_violated=False → violated when value < threshold
_DEFAULT_POLICIES: list[tuple[str, float, bool, Decision]] = [
    # PII in agent outputs
    ("pii_leak_rate",             0.0,  True,  Decision.WARN),
    ("pii_leak_rate",             0.10, True,  Decision.BLOCK),
    # PII arriving in agent inputs (informational)
    ("pii_input_rate",            0.0,  True,  Decision.WARN),
    # Prompt snapshot coverage
    ("prompt_snapshot_coverage",  1.0,  False, Decision.WARN),
    ("prompt_snapshot_coverage",  0.50, False, Decision.BLOCK),
]


def evaluate_policies(
    metrics: dict[str, float],
    policies: list[tuple[str, float, bool, Decision]] | None = None,
) -> list[PolicyResult]:
    """
    Evaluate a metrics dict against policy rules.

    For each policy rule, if the metric is present in `metrics`:
      - Computes whether the threshold is violated.
      - Emits a PolicyResult with the appropriate decision.

    When multiple rules apply to the same metric (e.g. pii_leak_rate has
    both a WARN and BLOCK rule), both are evaluated independently so the
    caller can see whether both fire or just one.

    Returns: list of PolicyResult, one entry per matching rule.
    """
    applied = policies if policies is not None else _DEFAULT_POLICIES
    results: list[PolicyResult] = []

    for metric, threshold, gt_violates, decision_on_violation in applied:
        value = metrics.get(metric)
        if value is None:
            continue

        if gt_violates:
            violated  = value > threshold
            direction = ">"
        else:
            violated  = value < threshold
            direction = "<"

        if violated:
            msg = (
                f"{metric} = {value:.4f}  {direction}  threshold {threshold:.4f}  →  {decision_on_violation.value.upper()}"
            )
            results.append(PolicyResult(
                metric=metric,
                decision=decision_on_violation,
                value=float(value),
                threshold=float(threshold),
                message=msg,
            ))
        else:
            msg = f"{metric} = {value:.4f}  OK  (threshold {direction} {threshold:.4f})"
            results.append(PolicyResult(
                metric=metric,
                decision=Decision.PASS,
                value=float(value),
                threshold=float(threshold),
                message=msg,
            ))

    return results
