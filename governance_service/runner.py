"""GovernanceRunner — Phase 1 + 2 + 3 checks for a completed trace.

  Phase 1: PII detection, prompt snapshot coverage
  Phase 2: token budget, prompt drift, model routing
  Phase 3: DB-backed policy evaluation, anomaly detection, automated remediation

Dedup: skips traces already in gov_audit_log with event_type='governance_eval'.
"""

import json
from dataclasses import dataclass, field

import structlog

from anomaly_detector import run_anomaly_checks
from budget_tracker import check_budget
from db import GovernanceDB
from drift_detector import check_drift
from model_router import get_routing_decision
from pii_scanner import scan_pii
from policy_store import load_policies
from prompt_checks import check_prompt_attributes
from remediation import build_remediations, run_remediations
from safety_guard import run_safety_checks, seed_safety_rules
from identity_guard import run_identity_checks
from reliability_tracker import check_reliability, seed_slo_config
from behavior_analyzer import run_behavior_checks
from lifecycle_tracker import run_lifecycle_checks
from incident_manager import auto_detect_incidents, write_incident_metrics
from regulatory_manager import compute_compliance_scorecard, get_risk_register
from enforcement_engine import run_enforcement_cycle

log = structlog.get_logger(__name__)


@dataclass
class GovernanceResult:
    trace_id:          str
    run_id:            str
    skipped:           bool          # True if already evaluated (dedup)
    pii_leak_rate:     float
    snapshot_coverage: float
    policy_blocks:     int
    policy_warnings:   int
    budget_exceeded:   list[str]     # agent_roles that exceeded budget
    drift_detected:    list[str]     # template_ids with drift
    routing_tier:      str           # complexity tier for this trace
    # Phase 4 summary fields
    injection_detected:   bool  = False
    jailbreak_detected:   bool  = False
    scope_violations:     int   = 0
    incidents_created:    int   = 0
    error_rate:           float = 0.0


class GovernanceRunner:
    """Evaluate governance checks for a single trace."""

    def __init__(self, db: GovernanceDB) -> None:
        self.db = db

    # ──────────────────────────────────────────────────────────────────────
    # Public API
    # ──────────────────────────────────────────────────────────────────────

    def evaluate(self, trace_id: str, run_id: str = "") -> GovernanceResult:
        """Run all governance checks for trace_id. Idempotent via dedup check."""
        if self._already_evaluated(trace_id):
            log.debug("governance_eval_skip_dedup", trace_id=trace_id)
            return GovernanceResult(
                trace_id=trace_id, run_id=run_id, skipped=True,
                pii_leak_rate=0.0, snapshot_coverage=0.0,
                policy_blocks=0, policy_warnings=0,
                budget_exceeded=[], drift_detected=[], routing_tier="",
            )

        spans = self.db.get_spans_for_trace(trace_id)
        if not spans:
            return GovernanceResult(
                trace_id=trace_id, run_id=run_id, skipped=True,
                pii_leak_rate=0.0, snapshot_coverage=0.0,
                policy_blocks=0, policy_warnings=0,
                budget_exceeded=[], drift_detected=[], routing_tier="",
            )

        # ── Phase 1: PII + prompt checks ────────────────────────────────
        pii_output_count   = 0
        pii_input_count    = 0
        snapshot_present   = 0
        template_versioned = 0
        total_spans        = len(spans)
        task_spans         = [s for s in spans if s.get("span_name") == "agent.task"]
        total_task         = len(task_spans) or 1

        agent_roles_seen: list[str] = []
        task_inputs_combined: list[str] = []

        for span in task_spans:
            attrs = span.get("attributes") or {}

            # PII in outputs
            output_text = str(attrs.get("task.output", "") or attrs.get("gen_ai.completion", "") or "")
            if output_text:
                pii_out = scan_pii(output_text)
                if pii_out.has_pii:
                    pii_output_count += 1
                    self.db.save_gov_metric(
                        trace_id, span["span_id"], run_id,
                        "pii_in_output", 1.0,
                        json.dumps({"pii_types": pii_out.pii_types}),
                    )

            # PII in inputs
            input_text = str(attrs.get("task.input", "") or attrs.get("gen_ai.prompt", "") or "")
            if input_text:
                pii_in = scan_pii(input_text)
                if pii_in.has_pii:
                    pii_input_count += 1
                    self.db.save_gov_metric(
                        trace_id, span["span_id"], run_id,
                        "pii_in_input", 1.0,
                        json.dumps({"pii_types": pii_in.pii_types}),
                    )
                task_inputs_combined.append(input_text)

            # Prompt snapshot coverage
            prompt_result = check_prompt_attributes(attrs)
            if prompt_result.snapshot_present:
                snapshot_present += 1
            if prompt_result.template_versioned:
                template_versioned += 1

            # Collect agent roles for budget check
            role = str(attrs.get("agent.role", "") or attrs.get("agent_role", "") or "")
            if role and role not in agent_roles_seen:
                agent_roles_seen.append(role)

        pii_leak_rate     = pii_output_count / total_task
        snapshot_coverage = snapshot_present  / total_task

        # Trace-level metric snapshots
        self.db.save_gov_metric(trace_id, "", run_id, "pii_leak_rate",         pii_leak_rate,     "")
        self.db.save_gov_metric(trace_id, "", run_id, "prompt_snapshot_coverage", snapshot_coverage, "")
        self.db.save_gov_metric(trace_id, "", run_id, "pii_output_count",      float(pii_output_count), "")
        self.db.save_gov_metric(trace_id, "", run_id, "pii_input_count",       float(pii_input_count),  "")

        # ── Load configurable thresholds once per run ───────────────────
        try:
            _thresholds = self.db.get_all_thresholds()
        except Exception:
            _thresholds = {}

        # ── Phase 2a: Token budget checks ───────────────────────────────
        budget_exceeded: list[str] = []
        budget_metrics: dict[str, float] = {}
        for role in agent_roles_seen:
            try:
                br = check_budget(self.db, role, thresholds=_thresholds)
                budget_metrics[f"budget_utilization.{role}"] = br.utilization
                self.db.save_gov_metric(
                    trace_id, "", run_id,
                    "budget_utilization", br.utilization,
                    json.dumps({"agent_role": role, "status": br.status,
                                "tokens_used": br.tokens_used,
                                "daily_limit": br.daily_limit}),
                )
                if br.status == "exceeded":
                    budget_exceeded.append(role)
            except Exception as exc:
                log.warning("budget_check_failed", agent_role=role, error=str(exc))

        # ── Phase 2b: Prompt drift detection ────────────────────────────
        drift_detected_ids: list[str] = []
        seen_templates: dict[str, str] = {}  # template_id → snapshot_hash

        for span in task_spans:
            attrs = span.get("attributes") or {}
            template_id   = str(attrs.get("prompt.template_id",   "") or "").strip()
            snapshot_hash = str(attrs.get("prompt.snapshot_hash", "") or "").strip()
            if template_id and snapshot_hash:
                seen_templates[template_id] = snapshot_hash

        for template_id, snapshot_hash in seen_templates.items():
            try:
                dr = check_drift(self.db, template_id, snapshot_hash)
                drift_flag = 1.0 if dr.is_drift else 0.0
                self.db.save_gov_metric(
                    trace_id, "", run_id,
                    "prompt_drift", drift_flag,
                    json.dumps({"template_id": template_id,
                                "snapshot_hash": snapshot_hash,
                                "baseline_hash": dr.baseline_hash,
                                "is_new_template": dr.is_new_template}),
                )
                if dr.is_drift:
                    drift_detected_ids.append(template_id)
            except Exception as exc:
                log.warning("drift_check_failed", template_id=template_id, error=str(exc))

        # ── Phase 2c: Model routing decision ────────────────────────────
        routing_tier = ""
        try:
            combined_input = " ".join(task_inputs_combined)
            routing_config = self.db.get_routing_config()
            rd = get_routing_decision(
                task_input=combined_input,
                tool_count=len(task_spans),
                is_multi_agent=(len(task_spans) > 1),
                routing_config=routing_config or None,
            )
            routing_tier = rd.complexity_tier
            self.db.save_gov_metric(
                trace_id, "", run_id,
                "routing_savings_pct", rd.estimated_savings_pct,
                json.dumps({"complexity_tier": rd.complexity_tier,
                            "model": rd.model,
                            "input_chars": rd.input_length_chars}),
            )
            agent_role_label = agent_roles_seen[0] if agent_roles_seen else ""
            self.db.save_routing_decision(
                trace_id=trace_id,
                run_id=run_id,
                agent_role=agent_role_label,
                complexity_tier=rd.complexity_tier,
                model=rd.model,
                input_chars=rd.input_length_chars,
                estimated_savings_pct=rd.estimated_savings_pct,
            )
        except Exception as exc:
            log.warning("routing_decision_failed", trace_id=trace_id, error=str(exc))

        # ── Build per-role span slices for Phase 4 ──────────────────────
        # Each agent.task span carries agent.role in its attributes.
        # Build a dict so Phase 4 checks can be run per role with only
        # that role's spans, rather than collapsing everything to [0].
        _role_spans: dict[str, list[dict]] = {}
        for s in task_spans:
            _attrs = s.get("attributes") or {}
            _r = str(_attrs.get("agent.role", "") or _attrs.get("agent_role", "") or "")
            if _r:
                _role_spans.setdefault(_r, []).append(s)
        # Fallback: if no roles tagged on spans, use collected list
        _roles_to_check: list[str] = list(_role_spans.keys()) or (
            agent_roles_seen if agent_roles_seen else [""]
        )
        # Primary role used for trace-level decisions (policy, anomaly, routing)
        agent_role_label = agent_roles_seen[0] if agent_roles_seen else ""

        # ── Phase 3a: DB-backed policy evaluation ───────────────────────
        metrics_snapshot = {
            "pii_leak_rate":            pii_leak_rate,
            "prompt_snapshot_coverage": snapshot_coverage,
            "budget_utilization":       max(budget_metrics.values(), default=0.0),
        }

        policy_rules   = load_policies(self.db, agent_role=agent_role_label)
        policy_blocks  = 0
        policy_warnings = 0

        for rule in policy_rules:
            val = metrics_snapshot.get(rule.metric)
            if val is None:
                continue
            op = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}. \
                 get(rule.direction, ">")
            violated = (
                (op == ">" and float(val) > rule.threshold) or
                (op == "<" and float(val) < rule.threshold) or
                (op == ">=" and float(val) >= rule.threshold) or
                (op == "<=" and float(val) <= rule.threshold)
            )
            if not violated:
                decision_str = "pass"
            else:
                decision_str = rule.decision  # "warn" | "block"
                if rule.decision == "block":
                    policy_blocks   += 1
                else:
                    policy_warnings += 1

            self.db.save_gov_policy_decision(
                trace_id=trace_id, run_id=run_id,
                metric=rule.metric, decision=decision_str,
                value=float(val), threshold=rule.threshold,
                message=rule.description,
            )

        # ── Phase 3b: Anomaly detection ──────────────────────────────────
        # Merge eval scores for this trace into metrics_snapshot so the anomaly
        # detector can z-score LLM-judged metrics alongside operational ones.
        # Uses avg per metric in case multiple eval runs exist for the trace.
        try:
            eval_rows = self.db.fetch_all(
                "SELECT metric, avg(score) AS score FROM otel.eval_scores "
                "WHERE trace_id = %(tid)s GROUP BY metric",
                {"tid": trace_id},
            )
            for er in (eval_rows or []):
                m = str(er.get("metric", "") or "")
                if m:
                    metrics_snapshot[m] = float(er.get("score", 0) or 0)
        except Exception as exc:
            log.warning("eval_scores_fetch_failed", trace_id=trace_id, error=str(exc))

        anomalies: list = []
        try:
            anomalies = run_anomaly_checks(
                db=self.db,
                trace_id=trace_id,
                run_id=run_id,
                agent_role=agent_role_label,
                metrics=metrics_snapshot,
                thresholds=_thresholds,
            )
        except Exception as exc:
            log.warning("anomaly_checks_failed", trace_id=trace_id, error=str(exc))

        # ── Phase 3c: Automated remediation ─────────────────────────────
        try:
            remed_actions = build_remediations(
                pii_detected=(pii_output_count > 0),
                budget_exceeded=budget_exceeded,
                drift_detected=drift_detected_ids,
                anomalies=anomalies,
            )
            run_remediations(self.db, trace_id, run_id, remed_actions)
        except Exception as exc:
            log.warning("remediation_failed", trace_id=trace_id, error=str(exc))

        # ── Phase 4: Per-role checks (safety, identity, reliability,
        #            behavior, lifecycle, incidents) ──────────────────────
        # Run once per agent role seen in this trace so every agent gets
        # its own row in gov_reliability_metrics, gov_safety_events, etc.
        safety_result      = None
        identity_result    = None
        reliability_result = None
        behavior_result    = None
        lifecycle_result   = None
        phase4_incidents:  list = []

        # Build a parent→children map so we can walk the full subtree for
        # each role. LLM call spans (openai.chat, call_llm, etc.) carry
        # gen_ai.request.model but have NO agent.role attribute — they are
        # children of agent.task spans. A role-only attribute filter misses
        # them entirely, breaking supply-chain and credential checks.
        _child_map: dict[str, list[str]] = {}
        for _s in spans:
            _pid = _s.get("parent_span_id", "")
            if _pid:
                _child_map.setdefault(_pid, []).append(_s.get("span_id", ""))

        def _subtree(root_ids: set) -> set:
            """Return the full set of span_ids in the subtree rooted at root_ids."""
            result: set = set(root_ids)
            queue = list(root_ids)
            while queue:
                sid = queue.pop()
                for child in _child_map.get(sid, []):
                    if child not in result:
                        result.add(child)
                        queue.append(child)
            return result

        for _role in _roles_to_check:
            _role_task_spans = _role_spans.get(_role, task_spans)
            # Walk the full subtree of this role's task spans so that child
            # spans (LLM calls, tool calls) are included even though they
            # carry no agent.role attribute.
            _role_root_ids  = {s.get("span_id", "") for s in _role_task_spans}
            _role_all_ids   = _subtree(_role_root_ids)
            _role_all_spans = [s for s in spans if s.get("span_id", "") in _role_all_ids] or spans

            # 4a: Safety
            try:
                _sr = run_safety_checks(
                    db=self.db, trace_id=trace_id, run_id=run_id,
                    agent_role=_role, spans=_role_task_spans,
                    thresholds=_thresholds,
                )
                if _role == agent_role_label:
                    safety_result = _sr
            except Exception as exc:
                log.warning("safety_checks_failed", trace_id=trace_id,
                            agent_role=_role, error=str(exc))

            # 4b: Identity
            try:
                _ir = run_identity_checks(
                    db=self.db, trace_id=trace_id, run_id=run_id,
                    agent_role=_role, spans=_role_all_spans,
                    thresholds=_thresholds,
                )
                if _role == agent_role_label:
                    identity_result = _ir
            except Exception as exc:
                log.warning("identity_checks_failed", trace_id=trace_id,
                            agent_role=_role, error=str(exc))

            # 4c: Reliability — queries DB directly by agent_role
            try:
                _rr = check_reliability(
                    db=self.db, agent_role=_role,
                    trace_id=trace_id, run_id=run_id,
                )
                if _role == agent_role_label:
                    reliability_result = _rr
            except Exception as exc:
                log.warning("reliability_checks_failed", trace_id=trace_id,
                            agent_role=_role, error=str(exc))

            # 4d: Behavior
            try:
                _br = run_behavior_checks(
                    db=self.db, trace_id=trace_id, run_id=run_id,
                    agent_role=_role, spans=_role_task_spans,
                    thresholds=_thresholds,
                )
                if _role == agent_role_label:
                    behavior_result = _br
            except Exception as exc:
                log.warning("behavior_checks_failed", trace_id=trace_id,
                            agent_role=_role, error=str(exc))

            # 4e: Lifecycle
            try:
                _lr = run_lifecycle_checks(
                    db=self.db, trace_id=trace_id, run_id=run_id,
                    agent_role=_role, spans=_role_all_spans,
                )
                if _role == agent_role_label:
                    lifecycle_result = _lr
            except Exception as exc:
                log.warning("lifecycle_checks_failed", trace_id=trace_id,
                            agent_role=_role, error=str(exc))

            # 4f: Incident detection
            try:
                _incidents = auto_detect_incidents(
                    db=self.db, trace_id=trace_id, run_id=run_id,
                    agent_role=_role,
                    anomalies=anomalies,
                    safety_result=safety_result if _role == agent_role_label else None,
                    reliability_result=reliability_result if _role == agent_role_label else None,
                    thresholds=_thresholds,
                    policy_blocks=policy_blocks if _role == agent_role_label else 0,
                )
                phase4_incidents.extend(_incidents)
                write_incident_metrics(self.db, trace_id, run_id, _role)
            except Exception as exc:
                log.warning("incident_detection_failed", trace_id=trace_id,
                            agent_role=_role, error=str(exc))

        # ── Derive Phase 4 summary scalars for audit log + result ────────
        _injection_detected = bool(
            safety_result and getattr(safety_result, "injection_detected", False)
        )
        _jailbreak_detected = bool(
            safety_result and getattr(safety_result, "jailbreak_detected", False)
        )
        _scope_violations = int(
            getattr(behavior_result, "scope_violations", 0) if behavior_result else 0
        )
        _incidents_created = len(phase4_incidents)
        _error_rate = float(
            getattr(reliability_result, "error_rate", 0.0) if reliability_result else 0.0
        )

        self.db.save_gov_audit_log(
            trace_id=trace_id, span_id="", run_id=run_id,
            event_type="governance_eval",
            detail=json.dumps({
                "pii_leak_rate":         pii_leak_rate,
                "snapshot_coverage":     snapshot_coverage,
                "policy_blocks":         policy_blocks,
                "policy_warnings":       policy_warnings,
                "budget_exceeded":       budget_exceeded,
                "drift_detected":        drift_detected_ids,
                "routing_tier":          routing_tier,
                "total_spans":           total_spans,
                "anomalies":             len(anomalies),
                "injection_detected":    _injection_detected,
                "jailbreak_detected":    _jailbreak_detected,
                "scope_violations":      _scope_violations,
                "incidents_created":     _incidents_created,
                "error_rate":            _error_rate,
            }),
        )

        log.info(
            "governance_eval_complete",
            trace_id=trace_id,
            pii_leak_rate=round(pii_leak_rate, 4),
            snapshot_coverage=round(snapshot_coverage, 4),
            policy_blocks=policy_blocks,
            policy_warnings=policy_warnings,
            budget_exceeded=budget_exceeded,
            drift_count=len(drift_detected_ids),
            routing_tier=routing_tier,
            anomalies=len(anomalies),
            injection_detected=_injection_detected,
            jailbreak_detected=_jailbreak_detected,
            scope_violations=_scope_violations,
            incidents_created=_incidents_created,
            error_rate=round(_error_rate, 4),
        )

        # ── Enforcement cycle (Phase 1) — run per unique agent role ─────
        for _role in _roles_to_check:
            try:
                run_enforcement_cycle(self.db, _role, thresholds=_thresholds)
            except Exception as exc:
                log.warning("enforcement_cycle_failed", agent_role=_role,
                            trace_id=trace_id, error=str(exc))

        return GovernanceResult(
            trace_id=trace_id, run_id=run_id, skipped=False,
            pii_leak_rate=pii_leak_rate,
            snapshot_coverage=snapshot_coverage,
            policy_blocks=policy_blocks,
            policy_warnings=policy_warnings,
            budget_exceeded=budget_exceeded,
            drift_detected=drift_detected_ids,
            routing_tier=routing_tier,
            injection_detected=_injection_detected,
            jailbreak_detected=_jailbreak_detected,
            scope_violations=_scope_violations,
            incidents_created=_incidents_created,
            error_rate=_error_rate,
        )

    # ──────────────────────────────────────────────────────────────────────
    # Private helpers
    # ──────────────────────────────────────────────────────────────────────

    def _already_evaluated(self, trace_id: str) -> bool:
        row = self.db.fetch_one(
            "SELECT count() AS cnt FROM otel.gov_audit_log "
            "WHERE trace_id = %(tid)s AND event_type = 'governance_eval'",
            {"tid": trace_id},
        )
        return bool(row and int(row.get("cnt", 0) or 0) > 0)
