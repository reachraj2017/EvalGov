# HITL Queue & Gate Check UI Reference

## Two HITL surfaces, one underlying table

Both the **Policy Engine tab** (AI Governance page) and the **HITL Approvals tab** (Governance Enforcement page) read from and write to the same ClickHouse table: `otel.gov_hitl_queue`. An action taken in either view is immediately reflected in the other on next refresh.

---

## Policy Engine tab — HITL Queue (page 10)

| Aspect | Detail |
|--------|--------|
| Data access | Direct ClickHouse query via `db.get_gov_hitl_queue(status=...)` |
| Pending display | Risk tier, action_type, trace_id, raw JSON payload via `st.json()` |
| Approve / Reject | Direct DB call `db.update_gov_hitl_decision()` |
| History tabs | Approved and Rejected shown as plain dataframes |
| Context enrichment | None — raw payload blob only |

**When to use:** Raw audit view; useful when debugging what exactly was written to the queue.

---

## Enforcement tab — HITL Approvals (page 11)

| Aspect | Detail |
|--------|--------|
| Data access | HTTP `GET /hitl/queue` via governance service API |
| Pending display | Priority context fields (query, prompt, input) surfaced as `st.info()` boxes via `_render_context()` |
| Approve / Reject | HTTP `PUT /hitl/{request_id}` |
| History | Decision history section with filters |
| Context enrichment | Yes — highlights the human-relevant fields so a reviewer can make an informed decision |

**When to use:** Operational review; designed for a human approver who needs to understand what the agent was doing before deciding.

---

## Gate Check Summary (Policy Engine tab)

Located below the HITL Queue in the Policy Engine tab. This is **not an approval queue** — it is a read-only audit trail.

- Source: `otel.gov_audit_log WHERE event_type = 'gate_check'`
- Written by `gate.py` on every `/gate/check` call, regardless of decision
- Each row's `detail` JSON contains: `action_type`, `agent_role`, `base_tier`, `final_tier`, `decision`, `escalations`
- The `decision` field reflects what the gate *computed* (auto_approve / flag / pause / block), not what was enforced

See [Gate Blocks vs Policy Blocks](#gate-blocks-vs-policy-blocks) below.

---

## Gate Blocks vs Policy Blocks

### Gate blocks (Gate Check Summary)

- Source: pre-execution gate (`gate.py` → `/gate/check` endpoint)
- Triggered by: risk tier escalation (CB open, quality gate hold/block, PII detection, budget exceeded)
- Decision values: `auto_approve`, `flag`, `pause`, `block`
- **`block` = the gate computed a critical-tier decision** (e.g. circuit breaker OPEN, or quality gate fired a block action)
- **Whether this actually stops the agent depends on enforcement being enabled.** With `enforcement.phase2_enabled = 0`, the gate may not be called at all, or the runner may ignore the result. The audit log records the *computed* decision regardless.

### Policy blocks (Policy Decisions section)

- Source: policy engine (`gov_policy_decisions` table), evaluated post-hoc by the governance runner
- Triggered by: policy rules configured in the Policy Engine tab (e.g. metric threshold crossing a rule condition)
- Decision values: `pass`, `warn`, `block`
- **`block` here is a policy verdict**, not a pre-execution gate. Currently recorded as a decision but not wired to actually stop an agent — it is an audit/compliance record.

### Summary

| | Gate Check Block | Policy Block |
|---|---|---|
| Timing | Pre-execution (before agent acts) | Post-hoc (after trace evaluated) |
| Source | `gate.py` risk tier logic | Policy engine rule match |
| Enforcement | Only active when `phase2_enabled = 1` | Recorded only; not enforced |
| Table | `gov_audit_log` (event_type=gate_check) | `gov_policy_decisions` |
| UI | Gate Check Summary dataframe | Policy Decisions section |

---

## Quality Gate Hold → HITL entry

When `quality_gate_checker.py` fires a `hold` decision, it calls `_create_hitl_entry()` which inserts into `gov_hitl_queue` with `action_type = quality_gate_hold`. This entry appears in both HITL surfaces. Approving or rejecting it from either view updates the same row.

---

## PII and Budget: Enforcement vs Policy Records

Both PII and budget appear as configurable policy rules in the Policy Engine tab. They also have **separate hardcoded enforcement paths** in `gate.py` that are independent of the policy engine.

### PII

**Detection (post-hoc, always on):**
The governance runner (`runner.py`) scans every span's output via `pii_scanner.py` after the trace completes. It writes `pii_leak_rate` and `pii_in_output` to `gov_metric_snapshots` regardless of enforcement state.

**Enforcement (pre-execution, via gate):**
`gate.py` step 2 queries `gov_metric_snapshots` for `pii_leak_rate > 0` on the current trace ID. If found, the risk tier is escalated by one level before the gate decision is made. This is what actually pauses or blocks the agent's next action.

The policy engine also records a PII verdict in `gov_policy_decisions` — but that record does not drive enforcement. The gate consumes the raw metric directly.

### Budget / Token Limit

**Tracking (continuous):**
The runner tracks token usage per agent in `gov_token_budgets` as traces are evaluated.

**Enforcement (pre-execution, via gate):**
`gate.py` step 3 calls `check_budget(db, agent_role)` on every gate check:
- `exceeded` → escalate tier by one level
- `warning` (>80% utilisation) and current tier is `low` → escalate to `medium`

Again, the policy engine records a budget verdict separately as an audit record — it is not what triggers enforcement.

### Why this matters for custom policies

PII and budget are enforced because `gate.py` has hardcoded escalation steps that query the relevant metrics directly. Custom policy rules (e.g. role_adherence rate below threshold, toxicity trend) have no equivalent step — the gate has no awareness of them. Wiring policy `block` verdicts back into the gate (a future capability) would close this gap for custom rules without duplicating the existing PII and budget logic.

| Signal | Gate enforcement | Policy verdict |
|--------|-----------------|----------------|
| PII detected | `gate.py` step 2 — direct metric query | Audit record only |
| Budget exceeded | `gate.py` step 3 — `check_budget()` call | Audit record only |
| Custom policy rule | ❌ Not wired today | Audit record only |
| Quality gate score | `gate.py` step 1b — `get_recent_quality_gate_blocks()` | Decision + HITL entry |
