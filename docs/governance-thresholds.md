# Governance Thresholds — Wiring & Configuration Guide

_Covers all 34 configurable thresholds in the AI governance system: where they are defined, how they flow into the enforcement and evaluation code, and how quickly changes take effect._

_Last updated: 2026-05-29_

---

## How Thresholds Are Defined

All thresholds are seeded into the `otel.gov_threshold_config` ClickHouse table on first startup. They are editable at any time via:

**AI Governance UI → Thresholds page** — change the value, click Save. No code change or redeploy required for most thresholds (see timing section below).

---

## Threshold Categories (34 total)

| Category | Count | Keys |
|---|---|---|
| Safety | 6 | `safety.injection_confidence`, `safety.jailbreak_confidence`, `safety.toxicity_score`, `safety.toxic_keyword_confidence`, `safety.bias_score`, `safety.bias_keyword_confidence` |
| Identity | 2 | `identity.session_ttl_seconds`, `identity.least_privilege_ratio` |
| Behavior | 3 | `behavior.consistency_decrement`, `behavior.min_output_length`, `behavior.ood_zscore_multiplier` |
| Anomaly | 4 | `anomaly.zscore_medium`, `anomaly.zscore_high`, `anomaly.zscore_critical`, `anomaly.min_samples` |
| Budget | 4 | `budget.warning_utilization`, `budget.exceeded_utilization`, `budget.input_token_cost_per_1m`, `budget.output_token_cost_per_1m` |
| Incident | 2 | `incident.error_budget_breach_multiplier`, `incident.dedup_window_days` |
| Regulatory | 7 | `regulatory.pii_pass_threshold`, `regulatory.audit_coverage_pass`, `regulatory.audit_coverage_partial`, `regulatory.prompt_snapshot_coverage`, `regulatory.availability_slo_pass`, `regulatory.compliance_partial_ratio`, `regulatory.compliance_partial_multiplier` |
| Enforcement | 6 | `enforcement.phase2_enabled`, `enforcement.quality_gates_enabled`, `cb.failure_threshold`, `cb.recovery_minutes`, `quality_gate.hold_to_block_threshold`, `quality_gate.hold_review_timeout_seconds` |

---

## Wiring Audit — Are All Thresholds Connected?

### Wiring Pattern

The standard flow is:

```
runner.py → db.get_all_thresholds() → thresholds dict
                                           ↓
                        safety_guard, identity_guard, behavior_analyzer,
                        anomaly_detector, budget_tracker, incident_manager,
                        regulatory_manager, enforcement_engine
```

`runner.py:149` calls `get_all_thresholds()` once per trace evaluation and passes the resulting dict to every module. Each module reads its values with a pattern like:

```python
t = thresholds or {}
threshold_value = float(t.get("safety.injection_confidence", 0.9))
```

### Audit Results

| Status | Count | Categories |
|---|---|---|
| ✅ Fully wired (no special handling) | 22 | Safety, identity, behavior, incident, regulatory, budget warning/exceeded |
| ⚠️ Wired with hardcoded fallback | 8 | Anomaly (4), circuit breaker (2), budget cost rates (2) |
| 🔁 Wired via direct DB read | 4 | Enforcement flags (`phase2_enabled`, `quality_gates_enabled`) and quality gate settings (`hold_to_block_threshold`, `hold_review_timeout_seconds`) |
| ❌ Orphaned (defined but never read) | 0 | None |

**All 34 thresholds are read by the code.** No threshold is defined in the UI but ignored.

### Wired with Hardcoded Fallback

These thresholds are read from the DB first, but fall back to a hardcoded default if the DB call fails:

| Threshold | Fallback | File |
|---|---|---|
| `anomaly.zscore_medium` | `2.0` | `anomaly_detector.py` |
| `anomaly.zscore_high` | `3.0` | `anomaly_detector.py` |
| `anomaly.zscore_critical` | `4.0` | `anomaly_detector.py` |
| `anomaly.min_samples` | `5` | `anomaly_detector.py` |
| `cb.failure_threshold` | `5` | `enforcement_engine.py` |
| `cb.recovery_minutes` | `30` | `enforcement_engine.py` |
| `budget.input_token_cost_per_1m` | `0.15` | `eval_runner/pipeline/eval_pipeline.py` |
| `budget.output_token_cost_per_1m` | `0.60` | `eval_runner/pipeline/eval_pipeline.py` |

Fallbacks only activate on DB connectivity failure — under normal operation these always read from the governance UI setting.

### Wired via Direct DB Read (Enforcement)

Four enforcement thresholds bypass the standard thresholds dict and query the DB directly on every request:

| Threshold | File | Method |
|---|---|---|
| `enforcement.phase2_enabled` | `gate.py` | `_phase2_enabled()` queries DB live |
| `enforcement.quality_gates_enabled` | `quality_gate_checker.py` | `_quality_gates_enabled()` queries DB live |
| `quality_gate.hold_to_block_threshold` | `quality_gate_checker.py` | `_get_cfg()` queries DB live |
| `quality_gate.hold_review_timeout_seconds` | `quality_gate_checker.py` | `_get_cfg()` queries DB live |

`cb.failure_threshold` and `cb.recovery_minutes` go through the standard `runner.py` thresholds dict (they are not direct DB reads), but also have hardcoded fallbacks in `enforcement_engine.py` — see the section above.

These are actually more responsive than the standard pattern — changes apply on the very next API call, not just the next trace.

---

## When Do Changes Take Effect?

### Immediate — next incoming trace (no restart required)

**30 standard thresholds** (all except the 4 direct-DB enforcement ones): safety, identity, behavior, anomaly, incident, regulatory, budget warning/exceeded utilization, and circuit breaker settings.

`runner.py` calls `get_all_thresholds()` fresh on every trace evaluation. Change the value in the UI → it applies to the very next trace that arrives in the system.

```
UI save → otel.gov_threshold_config updated → next trace → runner.py fetches fresh → applied
```

### Immediate — next API / gate check (no restart required)

**4 enforcement thresholds**: `phase2_enabled`, `quality_gates_enabled`, `hold_to_block_threshold`, `hold_review_timeout_seconds`.

These query the DB live on every request. Changes apply instantly — no trace needed to trigger the reload.

### Requires eval-runner restart

**2 budget cost thresholds only**: `budget.input_token_cost_per_1m` and `budget.output_token_cost_per_1m`.

`eval_pipeline.py` loads these once at the start of the first trace evaluation and caches them in memory for the lifetime of the service (`_ensure_cost_rates()` sets a flag after the first successful load and never re-fetches).

After changing either cost threshold in the UI:

```bash
docker compose up -d --force-recreate eval-runner
```

This is the **only threshold** in the entire system that requires a service restart.

> **Note on Ollama / local models:** Set both cost thresholds to `0.0` in the governance UI and restart eval-runner. All subsequent traces will show `$0.00` cost, accurately reflecting that local inference has no API cost.

---

## Threshold Default Values Reference

| Key | Default | Unit | Description |
|---|---|---|---|
| `safety.injection_confidence` | 0.90 | ratio | Prompt injection detection threshold |
| `safety.jailbreak_confidence` | 0.85 | ratio | Jailbreak attempt detection threshold |
| `safety.toxicity_score` | 0.50 | ratio | Toxicity content threshold |
| `safety.toxic_keyword_confidence` | 0.70 | ratio | Keyword-based toxicity confidence |
| `safety.bias_score` | 0.50 | ratio | Demographic bias detection threshold |
| `safety.bias_keyword_confidence` | 0.60 | ratio | Keyword-based bias confidence |
| `identity.session_ttl_seconds` | 3600 | seconds | Max session token age before flagging |
| `identity.least_privilege_ratio` | 0.50 | ratio | Unused tools ratio above which over-provisioned flag fires |
| `behavior.consistency_decrement` | 0.20 | ratio | Role consistency drop that triggers flag |
| `behavior.min_output_length` | 100 | chars | Minimum acceptable output length |
| `behavior.ood_zscore_multiplier` | 3.0 | multiplier | Out-of-distribution z-score multiplier |
| `anomaly.zscore_medium` | 2.0 | z-score | Z-score for medium anomaly severity |
| `anomaly.zscore_high` | 3.0 | z-score | Z-score for high anomaly severity |
| `anomaly.zscore_critical` | 4.0 | z-score | Z-score for critical anomaly severity |
| `anomaly.min_samples` | 5 | count | Minimum baseline samples for anomaly detection |
| `budget.warning_utilization` | 0.80 | ratio | Token budget utilization to trigger warning |
| `budget.exceeded_utilization` | 1.00 | ratio | Token budget utilization to trigger exceeded |
| `budget.input_token_cost_per_1m` | 0.15 | USD | Cost per 1M input tokens (gpt-4o-mini default) |
| `budget.output_token_cost_per_1m` | 0.60 | USD | Cost per 1M output tokens (gpt-4o-mini default) |
| `incident.error_budget_breach_multiplier` | 2.0 | multiplier | Error rate multiplier to trigger incident |
| `incident.dedup_window_days` | 30 | days | Incident deduplication window |
| `regulatory.pii_pass_threshold` | 0.01 | ratio | PII leak rate at or below which GDPR/HIPAA compliance passes |
| `regulatory.audit_coverage_pass` | 0.95 | ratio | Audit log coverage for full compliance |
| `regulatory.audit_coverage_partial` | 0.50 | ratio | Audit log coverage for partial compliance |
| `regulatory.prompt_snapshot_coverage` | 0.95 | ratio | Prompt snapshot coverage threshold |
| `regulatory.availability_slo_pass` | 0.99 | ratio | Availability SLO pass threshold |
| `regulatory.compliance_partial_ratio` | 0.50 | ratio | Lower-bound ratio for partial compliance status |
| `regulatory.compliance_partial_multiplier` | 2.0 | multiplier | Upper-bound multiplier for partial compliance status |
| `enforcement.phase2_enabled` | 0 | bool | 1 = enable pre-execution gate checks; 0 = observe only |
| `enforcement.quality_gates_enabled` | 0 | bool | 1 = enable content quality gate enforcement; 0 = observe only |
| `cb.failure_threshold` | 5 | count | High/critical incidents in last 1h before circuit breaker opens |
| `cb.recovery_minutes` | 30 | minutes | Minutes before OPEN circuit breaker probes with HALF_OPEN |
| `quality_gate.hold_to_block_threshold` | 5 | count | Confirmed/expired holds before auto-block fires |
| `quality_gate.hold_review_timeout_seconds` | 300 | seconds | Seconds before unreviewed hold is auto-expired |
