"""GovernanceRunner — orchestrates all Phase 1 governance checks per trace.

Called after the eval pipeline completes for a trace. Runs:
  1. PII scan on task.input and task.output of every agent.task span.
  2. Prompt snapshot completeness check (prompt.snapshot_hash present?).
  3. Aggregate trace-level governance metrics (pii_leak_rate, snapshot_coverage).
  4. Policy evaluation against those metrics.
  5. Writes gov_metric_snapshots, gov_policy_decisions, gov_audit_log rows.

Failures are caught and logged — governance checks must never crash the
main eval pipeline.
"""

import json
from typing import TYPE_CHECKING

import structlog

from governance.pii_scanner import scan_pii
from governance.policy_engine import evaluate_policies, Decision
from governance.prompt_checks import check_prompt_attributes

if TYPE_CHECKING:
    from db.repository import Repository

log = structlog.get_logger(__name__)


class GovernanceRunner:
    """Run governance checks on a completed trace and persist results."""

    def __init__(self, repository: "Repository") -> None:
        self._repo = repository
        self._ensure_tables()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def evaluate(self, trace_id: str, run_id: str) -> None:
        """
        Run all governance checks for a trace.

        Args:
            trace_id: The trace to evaluate.
            run_id:   The eval run this trace belongs to (may be "").
        """
        try:
            self._run_inner(trace_id, run_id)
        except Exception as exc:
            log.error("governance_eval_failed", trace_id=trace_id, error=str(exc))

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _ensure_tables(self) -> None:
        """Create governance tables if they don't exist (idempotent)."""
        try:
            self._repo.ensure_gov_tables()
        except Exception as exc:
            log.warning("gov_ensure_tables_failed", error=str(exc))

    def _run_inner(self, trace_id: str, run_id: str) -> None:
        spans      = self._repo.get_trace_spans(trace_id)
        task_spans = [s for s in spans if s.get("span_name") == "agent.task"]

        if not task_spans:
            log.debug("gov_no_task_spans", trace_id=trace_id)
            return

        n = len(task_spans)
        per_span: dict[str, dict[str, float]] = {}

        for span in task_spans:
            span_id = span.get("span_id", "")
            attrs   = span.get("attributes", {})

            # ── PII checks ────────────────────────────────────────────
            pii_input  = scan_pii(str(attrs.get("task.input",  "") or ""))
            pii_output = scan_pii(str(attrs.get("task.output", "") or ""))

            # ── Prompt snapshot check ─────────────────────────────────
            prompt_check = check_prompt_attributes(attrs)

            per_span[span_id] = {
                "pii_in_input":              1.0 if pii_input.has_pii   else 0.0,
                "pii_in_output":             1.0 if pii_output.has_pii  else 0.0,
                "prompt_snapshot_present":   1.0 if prompt_check.snapshot_present   else 0.0,
                "prompt_template_versioned": 1.0 if prompt_check.template_versioned else 0.0,
            }

            # Build detail JSON (no actual PII values — only type names)
            detail: dict = {}
            if pii_output.has_pii:
                detail["pii_types_in_output"] = pii_output.pii_types
            if pii_input.has_pii:
                detail["pii_types_in_input"]  = pii_input.pii_types
            if not prompt_check.snapshot_present:
                detail["missing"] = detail.get("missing", []) + ["prompt.snapshot_hash"]
            if not prompt_check.template_versioned:
                detail["missing"] = detail.get("missing", []) + ["prompt.template_id"]

            # ── Persist per-span metrics ──────────────────────────────
            detail_json = json.dumps(detail) if detail else ""
            for metric, value in per_span[span_id].items():
                self._repo.save_gov_metric(
                    trace_id=trace_id,
                    span_id=span_id,
                    run_id=run_id,
                    metric=metric,
                    value=value,
                    detail=detail_json if (metric in ("pii_in_output", "pii_in_input",
                                                       "prompt_snapshot_present") and detail) else "",
                )

        # ── Aggregate trace-level governance metrics ──────────────────
        pii_output_count = sum(v["pii_in_output"]            for v in per_span.values())
        pii_input_count  = sum(v["pii_in_input"]             for v in per_span.values())
        snapshot_count   = sum(v["prompt_snapshot_present"]  for v in per_span.values())

        trace_metrics = {
            "pii_leak_rate":            pii_output_count / n,
            "pii_input_rate":           pii_input_count  / n,
            "prompt_snapshot_coverage": snapshot_count   / n,
        }

        # Persist trace-level aggregates (span_id="" = trace scope)
        for metric, value in trace_metrics.items():
            self._repo.save_gov_metric(
                trace_id=trace_id,
                span_id="",
                run_id=run_id,
                metric=metric,
                value=value,
                detail="",
            )

        # ── Policy evaluation ─────────────────────────────────────────
        decisions = evaluate_policies(trace_metrics)

        blocks   = sum(1 for d in decisions if d.decision == Decision.BLOCK)
        warnings = sum(1 for d in decisions if d.decision == Decision.WARN)
        passes   = sum(1 for d in decisions if d.decision == Decision.PASS)

        for d in decisions:
            self._repo.save_gov_policy_decision(
                trace_id=trace_id,
                run_id=run_id,
                metric=d.metric,
                decision=d.decision.value,
                value=d.value,
                threshold=d.threshold,
                message=d.message,
            )

        # ── Audit log ─────────────────────────────────────────────────
        self._repo.save_gov_audit_log(
            trace_id=trace_id,
            span_id="",
            run_id=run_id,
            event_type="governance_eval",
            detail=json.dumps({
                "task_span_count":    n,
                "pii_leak_rate":      round(trace_metrics["pii_leak_rate"],            4),
                "pii_input_rate":     round(trace_metrics["pii_input_rate"],           4),
                "snapshot_coverage":  round(trace_metrics["prompt_snapshot_coverage"], 4),
                "policy_blocks":      blocks,
                "policy_warnings":    warnings,
                "policy_passes":      passes,
            }),
        )

        log.info(
            "governance_eval_complete",
            trace_id=trace_id,
            task_spans=n,
            blocks=blocks,
            warnings=warnings,
            pii_leak_rate=round(trace_metrics["pii_leak_rate"], 4),
            snapshot_coverage=round(trace_metrics["prompt_snapshot_coverage"], 4),
        )
