"""
Regression comparator.
Compares a run's scores against the baseline run.
"""

from typing import Optional

import structlog

log = structlog.get_logger(__name__)

_REGRESSION_THRESHOLD = 0.05  # default: 5-point drop = regression


class RegressionComparator:
    """Computes metric deltas between an eval run and its baseline."""

    def compare(
        self,
        run_id: str,
        baseline_run_id: str,
        repository,
        threshold: float = _REGRESSION_THRESHOLD,
    ) -> list[dict]:
        """
        Compare per-metric average scores for run vs baseline.

        Returns a list of dicts, one per metric present in both runs:
        {
            metric:          str,
            current_score:   float,
            baseline_score:  float,
            delta:           float,   # current - baseline
            regressed:       bool,    # True if delta < -threshold
        }
        """
        try:
            raw = repository.get_regression(run_id, baseline_run_id)
        except Exception as exc:
            log.error(
                "regression_compare_failed",
                run_id=run_id,
                baseline_run_id=baseline_run_id,
                error=str(exc),
            )
            return []

        comparisons: list[dict] = []
        for row in raw:
            delta = float(row.get("delta", 0.0))
            comparisons.append(
                {
                    "metric": row["metric"],
                    "current_score": float(row.get("current_score", 0.0)),
                    "baseline_score": float(row.get("baseline_score", 0.0)),
                    "delta": delta,
                    "regressed": delta < -threshold,
                }
            )

        log.info(
            "regression_compare_done",
            run_id=run_id,
            baseline_run_id=baseline_run_id,
            metric_count=len(comparisons),
            regressed=[c["metric"] for c in comparisons if c["regressed"]],
        )
        return comparisons

    def get_regression_summary(self, comparisons: list[dict]) -> dict:
        """
        Aggregate regression comparison into a summary dict.

        Returns:
        {
            total_metrics:    int,
            regressed_count:  int,
            improved_count:   int,
            unchanged_count:  int,
            worst_regression: {metric, delta} | None,
        }
        """
        total = len(comparisons)
        regressed = [c for c in comparisons if c["regressed"]]
        improved = [c for c in comparisons if c["delta"] > 0]
        unchanged = [
            c for c in comparisons if not c["regressed"] and c["delta"] <= 0
        ]

        worst: Optional[dict] = None
        if regressed:
            worst_item = min(regressed, key=lambda c: c["delta"])
            worst = {"metric": worst_item["metric"], "delta": worst_item["delta"]}

        return {
            "total_metrics": total,
            "regressed_count": len(regressed),
            "improved_count": len(improved),
            "unchanged_count": len(unchanged),
            "worst_regression": worst,
        }
