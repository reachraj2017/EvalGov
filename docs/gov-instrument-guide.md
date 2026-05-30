# Governance Instrumentation Guide — Pre-Execution Enforcement

How to instrument agent systems with the `GovernedToolkit` SDK for
pre-execution governance enforcement.

> **Enforcement is a single capability in two layers.** "Phase 1" refers to
> the reactive layer — trust scores, burn rates, rogue detection, circuit
> breakers — which detects and responds to violations after they happen.
> "Phase 2" refers to the preventive layer — the gate check SDK — which
> blocks agents *before* they run based on the signals Phase 1 has already
> computed. Both layers are part of the same enforcement system and run
> together. This guide covers Phase 2 instrumentation.

---

## How Enforcement Works

Phase 1 (trust scores, burn rates, rogue detection, circuit breakers) is **reactive** —
it detects and responds to violations after they happen.

Phase 2 is **preventive** — it blocks agents *before* they run. Enforcement happens at
two layers:

**Layer 1 — Runner level (pre-LLM):** checked before the agent's LLM is called at all.
An OPEN circuit breaker stops the agent immediately — no tokens spent, no LLM call made.
This covers all agent activity including direct answers, meta-questions, and anything that
doesn't call a tool.

**Layer 2 — Tool level (pre-execution):** checked via `before_tool_callback` before each
tool function is invoked. A secondary safety net for any tool call that reaches this point.

```
Phase 1 (observe + respond)          Phase 2 (prevent — two layers)
─────────────────────────────         ──────────────────────────────────────────────
Trace arrives                         Request arrives for agent
   ↓                                     ↓
Watcher evaluates (30s later)         LAYER 1: runner checks circuit breaker
   ↓                                     ↓ CB OPEN → block immediately (no LLM call)
Trust score updated                      ↓ CB closed → LLM runs
   ↓                                     ↓
Circuit breaker may open              Agent wants to call a tool
   ↓                                     ↓
Incident created                      LAYER 2: GovernedToolkit.gate_check()
                                         ↓
                                      governance-service /gate/check
                                         ↓ (CB state, PII, budget, trust tier)
                                         ↓
                                      allow → tool runs
                                      block → tool never called
```

---

## The Enable/Disable Flag

Phase 2 enforcement is off by default. Turning it on is a one-click toggle in the
**Governance Enforcement** page — no restart required.

| State | Behaviour |
|---|---|
| **Disabled** (default) | Gate check endpoint still runs + logs decisions, but circuit breaker does not block calls. Observe-only. |
| **Enabled** | Circuit breaker state is checked on every gate request. OPEN circuit breaker → immediate block. HALF_OPEN → tier escalation. |

**Backend flag:** `enforcement.phase2_enabled` in `otel.gov_threshold_config`.
Value `0` = disabled, `1` = enabled.

**UI toggle:** Governance Enforcement page → "Phase 2 Pre-Execution Enforcement" panel.

**API:**
```bash
# Check status
curl http://localhost:8002/enforcement/phase2/status

# Enable
curl -X POST http://localhost:8002/enforcement/phase2/enable

# Disable
curl -X POST http://localhost:8002/enforcement/phase2/disable
```

The backend flag is the single source of truth. The SDK always calls the gate;
the gate returns `auto_approve` when phase2 is disabled and `block` when the
circuit breaker is OPEN. No client-side flag is needed.

An agent without the SDK is still observed via OTel but not blocked pre-execution.

---

## SDK Installation

The `GovernedToolkit` lives in the `instrumentation` package at the repo root.

```bash
# From the repo root — no separate install needed if running in-repo
# The instrumentation/ package is importable as long as the repo root is on sys.path

# Verify the repo root is on your PYTHONPATH (or add it):
export PYTHONPATH=/path/to/aieval:$PYTHONPATH
```

Required dependencies (already in `instrumentation/requirements.txt`):
```
httpx>=0.27.0
opentelemetry-api>=1.24.0
```

Uses Python stdlib `logging` — no `structlog` required.

---

## Integration Patterns

### Pattern 1 — Wrap a callable (any framework)

The simplest integration. Wraps any tool function so every call is gate-checked first.

```python
from instrumentation.governed_toolkit import GovernedToolkit

toolkit = GovernedToolkit(agent_role="searcher")

# Wrap once at module level
governed_search = toolkit.wrap("web_search", web_search)

# Use exactly like the original function
result = governed_search(query="latest AI news")
```

If the gate blocks, `GateBlockedError` is raised. Handle it like any other exception:

```python
from instrumentation.governed_toolkit import GovernedToolkit, GateBlockedError

try:
    result = governed_search(query="...")
except GateBlockedError as exc:
    # Tool was blocked — log, return fallback, surface to user
    print(f"Tool blocked: {exc}")
    result = "Unable to complete — governance gate blocked this action."
```

---

### Pattern 2 — Inline gate check

For cases where you control the call site directly.

```python
toolkit = GovernedToolkit(agent_role="data-analyst")

# Gate check + call in one line
result = toolkit.call("delete_records", delete_records, table="users", before="2020-01-01")

# Or gate check separately (e.g. to inspect the decision before calling)
gate_result = toolkit.gate_check("delete_records")
if gate_result:
    print(f"Decision: {gate_result['decision']}, tier: {gate_result['risk_tier']}")
result = delete_records(table="users", before="2020-01-01")
```

---

### Pattern 3 — Decorator

Best for functions that are always governed the same way.

```python
from instrumentation.governed_toolkit import GovernedToolkit

toolkit = GovernedToolkit(agent_role="finance-agent")

# Decorate at definition time
governed_search = toolkit.wrap("web_search", web_search)

# Or use the lower-level GateClient.governed decorator for function decoration:
from instrumentation.gate_client import governed

@governed("send_email", agent_role="emailer")
def send_invoice_email(to: str, subject: str, body: str): ...
```

---

### Pattern 4 — Runner-level gate check (pre-LLM)

This is the primary enforcement layer. Add it to whatever function dispatches agent
invocations — before any LLM call is made. Covers direct answers, meta-questions, and
any agent behaviour that doesn't call a tool.

The full pattern (as used in `examples/opt-demo/runner.py`) calls `/gate/check` rather
than just polling circuit breaker state directly. This means HITL pause decisions are
also handled at the runner level, not only at the tool level.

> **Critical: gate every execution path, not just the LLM path.**
>
> If your agent system has multiple execution paths — an LLM/API path and a local
> model path (SLM, HuggingFace, ONNX, etc.) — each path must call `_agent_gate_check()`
> independently before doing any work. The gate check only runs when execution flows
> through the function that contains it.
>
> Example: `runner.py` has `_call_agent()` (GPT-4 path) and `_slm_search()` / `_slm_translate()` /
> `_slm_summarize()` (SLM path). Both paths require their own gate check at the top:
>
> ```python
> async def _slm_search(topic, ...):
>     # Gate check BEFORE the SLM or any tool runs
>     if _phase2_enabled():
>         decision, request_id = _agent_gate_check("searcher", user_query=topic, ...)
>         if decision == "block":
>             return "[GOVERNANCE BLOCK] searcher blocked by circuit breaker."
>         if decision == "pause":
>             outcome = _wait_for_hitl_agent_approval(request_id, "searcher")
>             if outcome != "approved":
>                 return f"[GOVERNANCE BLOCK] searcher requires approval — {outcome}."
>     # SLM execution only reaches here if gate allowed it
>     raw_results = web_search(topic)
>     answer, _, _ = synthesize(topic, raw_results)
>     ...
> ```
>
> A quarantined agent running in local model mode will bypass all enforcement if only
> `_call_agent()` has the gate check. The local model path must be gated explicitly.

```python
import httpx, time, os

_GOV_URL  = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
_CB_CACHE: dict[str, tuple[str, float]] = {}
_CB_TTL   = 30.0

def _agent_gate_check(
    agent_role: str,
    user_query: str = "",
    trace_id: str = "",
    run_id: str = "",
) -> tuple[str, str]:
    """Call the governance gate. Returns (decision, request_id).
    decision: auto_approve | flag | pause | block
    """
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.post(
                f"{_GOV_URL}/gate/check",
                json={
                    "action_type": "agent_invoke",
                    "agent_role":  agent_role,
                    "context":     {"agent": agent_role, "query": user_query[:500]},
                    "trace_id":    trace_id,
                    "run_id":      run_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("decision", "auto_approve"), data.get("request_id", "")
    except Exception:
        return "auto_approve", ""  # fail open

def _wait_for_hitl_approval(request_id: str, timeout: float = 300.0) -> str:
    """Poll until operator approves/rejects. Returns 'approved', 'rejected', or 'timeout'."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(f"{_GOV_URL}/hitl/{request_id}/status")
                if resp.status_code == 200:
                    status = resp.json().get("status", "pending")
                    if status == "approved":
                        return "approved"
                    if status in ("rejected", "expired"):
                        return status
        except Exception:
            pass
        time.sleep(3.0)
    return "timeout"

async def call_agent(agent_name: str, message: str, trace_id: str, run_id: str, ...) -> str:
    if _phase2_enabled():
        decision, request_id = _agent_gate_check(agent_name, message, trace_id, run_id)
        if decision == "block":
            return f"[GOVERNANCE BLOCK] {agent_name} blocked by circuit breaker."
        if decision == "pause":
            outcome = _wait_for_hitl_approval(request_id)
            if outcome != "approved":
                return f"[GOVERNANCE BLOCK] {agent_name} requires human approval — {outcome}."
    # ... proceed with LLM call
```

**Simpler alternative (CB-only, no HITL at runner level):** If you only need circuit-breaker blocking and handle HITL at the tool level, you can poll CB state directly:

```python
def _get_cb_state(agent_role: str) -> str:
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{_GOV_URL}/enforcement/circuit-breakers")
            resp.raise_for_status()
            for row in resp.json():
                if row.get("agent_role") == agent_role:
                    return str(row.get("state", "closed"))
    except Exception:
        pass
    return "closed"
```

The 30-second cache (`_CB_CACHE`) avoids a governance service round-trip on every request
while staying responsive to circuit breaker state changes.

---

### Pattern 5 — Google ADK (before_tool_callback)

The ADK integration hooks into the framework's before_tool_callback mechanism.
The tool function is never invoked when the gate blocks — ADK treats the callback's
return value as the tool's output.

```python
from instrumentation.governed_toolkit import GovernedToolkit
from google.adk.agents import LlmAgent

toolkit = GovernedToolkit(agent_role="searcher")

searcher = LlmAgent(
    name="searcher",
    model=model,
    tools=[web_search],
    before_tool_callback=toolkit.adk_before_tool_callback(),
    # ... other config unchanged
)
```

When blocked, the agent receives `[GOVERNANCE BLOCK] <reason>` as the tool result
and can respond to the user accordingly (e.g. "I was unable to search because...").

**Combining with existing before_tool_callback:**

If you already have a before_tool_callback for span instrumentation or routing logic,
wrap both:

```python
_existing_cb = _before_tool_cb   # your existing callback
_gate_cb     = toolkit.adk_before_tool_callback()

def _combined_before_tool(tool, args, tool_context):
    # Gate check first — if blocked, returns non-None (tool is skipped)
    gate_block = _gate_cb(tool, args, tool_context)
    if gate_block is not None:
        return gate_block
    # Then your existing instrumentation
    return _existing_cb(tool, args, tool_context)

agent = LlmAgent(..., before_tool_callback=_combined_before_tool)
```

---

### Pattern 6 — LangChain

Wrap tools before passing them to the agent:

```python
from langchain.tools import Tool
from instrumentation.governed_toolkit import GovernedToolkit, GateBlockedError

toolkit = GovernedToolkit(agent_role="research-agent")

def _safe_search(query: str) -> str:
    try:
        return toolkit.call("web_search", web_search, query=query)
    except GateBlockedError as exc:
        return f"[BLOCKED] {exc}"

governed_tool = Tool(
    name="web_search",
    func=_safe_search,
    description="Search the web for current information.",
)

agent = initialize_agent(tools=[governed_tool], ...)
```

---

### Pattern 7 — AutoGen

Wrap the tool function before registering it as a reply function:

```python
from instrumentation.governed_toolkit import GovernedToolkit

toolkit = GovernedToolkit(agent_role="autogen-worker")
governed_search = toolkit.wrap("web_search", web_search)

# Register the governed version
agent.register_function(
    function_map={"web_search": governed_search}
)
```

---

## Configuration Reference

| Env var | Default | Description |
|---|---|---|
| `GOVERNANCE_SERVICE_URL` | `http://localhost:8002` | Governance service endpoint |
| `GOVERNANCE_AGENT_KEY` | *(empty)* | API key for authenticated gate checks (optional unless auth is enforced) |
| `GATE_TIMEOUT_SECONDS` | `3.0` | Gate check HTTP timeout. Keep ≤5s — LLM calls are 200–2000ms, gate should not dominate. |
| `GATE_FAIL_OPEN` | `true` | If `true`, governance service unavailability does not block agents. Set `false` in high-security environments. |

The enforcement on/off decision lives **only on the backend** (`enforcement.phase2_enabled` threshold flag). There is no client-side toggle — the SDK always calls the gate and the gate decides.

---

## What Happens When Enforcement is Off

When Phase 2 is disabled on the backend (`enforcement.phase2_enabled = 0`):

- Layer 1 (runner): `_agent_gate_check()` still calls the gate, but the gate returns `auto_approve` — no blocking occurs
- Layer 2 (tool): `GovernedToolkit.gate_check()` calls the backend gate, which returns `auto_approve` — tool always proceeds
- No blocks occur; gate decisions are logged but not enforced

This means you can instrument your agents **now** and enable enforcement later with a single API call — no agent code changes required.

---

## Quality Gate Decisions

Post-execution quality gate decisions are written to `otel.gov_quality_gate_decisions` after each eval run by the eval-runner. These are separate from the pre-execution gate check above.

**Decision lifecycle:**

| Status | Meaning |
|---|---|
| `pending` | Decision written by eval-runner; not yet reviewed by an operator |
| `reviewed` | Operator marked the decision as reviewed |
| `overridden` | Operator accepted the output despite a threshold breach |

**Current state (as of 2026-05-26):** All decisions are written with `status = 'pending'`. The review workflow (`PUT /quality-gates/decisions/{id}`, operator UI buttons) has not been built yet. Every decision in the Decision Log will show `pending` — this is expected and has no operational effect while enforcement is observe-only.

**Indirect enforcement path:** `get_recent_quality_gate_blocks()` queries `gov_quality_gate_decisions WHERE action IN ('block','hold') AND status = 'pending'`. These feed as escalation signals into future pre-execution gate checks (`/gate/check`). With enforcement enabled and a high volume of `block`/`hold` decisions from poor eval scores, gate checks will escalate to HITL or block for subsequent agent requests for the same agent role.

**Before enabling quality gate enforcement:** ensure the review workflow exists so that `pending` decisions can be resolved — otherwise past quality failures permanently raise the escalation signal for that agent role.

---

## OTel Span Attributes Added

When enforcement is active and the gate check runs, these attributes are stamped on the
`agent.tool_call` span:

| Attribute | Values | Description |
|---|---|---|
| `gate.enforcement` | `true` / `false` | Whether Phase 2 enforcement was active |
| `gate.decision` | `auto_approve` / `flag` / `pause` / `block` | Gate decision |
| `gate.risk_tier` | `low` / `medium` / `high` / `critical` | Risk classification |
| `gate.request_id` | UUID or empty | HITL queue entry ID (when decision = pause) |
| `gate.tool` | tool name string | Which tool triggered the check |

These are visible in Jaeger traces and queryable in ClickHouse:

```sql
SELECT
    SpanAttributes['gate.decision']   AS decision,
    SpanAttributes['gate.risk_tier']  AS risk_tier,
    SpanAttributes['tool.name']       AS tool,
    SpanAttributes['agent.role']      AS agent_role,
    count() AS count
FROM otel.otel_traces
WHERE SpanName = 'agent.tool_call'
  AND SpanAttributes['gate.enforcement'] = 'true'
GROUP BY decision, risk_tier, tool, agent_role
ORDER BY count DESC
```

---

## How the Gate Decision is Made

The gate evaluates each tool call in this order:

```
1. Is Phase 2 enforcement enabled?
      No  → auto_approve (pass-through)
      Yes → continue

2. Is this agent's circuit breaker OPEN?
      Yes → block immediately (no further evaluation)
      HALF_OPEN → escalate risk tier by one level, continue

3. Has this trace recently leaked PII?
      Yes → escalate tier by one level

4. Has this agent exceeded its token budget?
      Exceeded → escalate tier by one level
      Near limit (>80%) and tier=low → escalate to medium

5. Apply final tier decision:
      low      → auto_approve
      medium   → flag (proceed + async review queued)
      high     → pause (block until human approves in HITL queue)
      critical → block (hard block)
```

**Tool-to-action-type mapping** (controls base risk tier):

| Tool name pattern | Action type | Default tier |
|---|---|---|
| `web_search`, `search`, `fetch` | `search` / `fetch` | low |
| `read_file`, `list_files` | `read` / `list` | low |
| `write_file`, `create_file`, `update_file` | `write` / `create` / `update` | medium |
| `send_email`, `send_message`, `post` | same | medium |
| `delete_file`, `delete`, `remove` | `delete` / `remove` | high |
| `execute_code`, `run_script` | `execute_code` | high |
| `publish`, `deploy` | same | high |
| *(any unrecognised tool name)* | `tool_call` | medium |

Add custom mappings to `_TOOL_ACTION_MAP` in `governed_toolkit.py`.

---

## Instrumentation Checklist

Use this checklist when adding governance enforcement to a new agent:

```
□ Governance service is running (docker-compose up -d governance-service)
□ GOVERNANCE_SERVICE_URL is set correctly in the agent's environment
□ Phase 2 enabled on backend: POST /enforcement/phase2/enable  (no client flag needed)

LAYER 1 — Runner level (pre-LLM):
□ _agent_gate_check(agent_role, ...) called at the agent dispatch entry point (calls /gate/check)
□ Returns block message immediately if decision == "block" — before LLM call
□ Polls HITL approval endpoint if decision == "pause"
□ If agent has a local model / SLM path (non-LLM): gate check added to that path too
  (local model paths bypass _call_agent() entirely — each path needs its own gate check)

LAYER 2 — Tool level (secondary):
□ GovernedToolkit instantiated with the correct agent_role
□ All external tool calls wrapped: toolkit.wrap() / toolkit.call() / adk_before_tool_callback()
□ GateBlockedError caught and handled (return block message to user, do not fall back to training data)
□ Agent instruction explicitly forbids training-data fallback when tool returns [GOVERNANCE BLOCK]

OBSERVABILITY:
□ Agent role is registered in AI Governance → Tool Whitelist
□ SLO config is set for the agent role in AI Governance → SLO Configuration
□ Run one enforcement cycle for the agent (On-Demand panel) to seed circuit breaker record
□ Trust score appears in Governance Enforcement → Agent Trust Scores after first run
□ gate.decision and gate.enforcement appear on agent.tool_call spans in Jaeger
```

---

## Current Instrumented Agents

| Agent | Demo | Layer 1 — LLM path | Layer 1 — SLM path | Layer 2 (tool) |
|---|---|---|---|---|
| `orchestrator` | opt-demo | `_agent_gate_check()` in `_call_agent()` | — (no SLM path) | — (no tools) |
| `searcher` | opt-demo | `_agent_gate_check()` in `_call_agent()` | `_agent_gate_check()` in `_slm_search()` | `_before_tool_cb` + `GovernedToolkit.gate_check()` |
| `summarizer` | opt-demo | `_agent_gate_check()` in `_call_agent()` | `_agent_gate_check()` in `_slm_summarize()` | — (no tools) |
| `translator` | opt-demo | `_agent_gate_check()` in `_call_agent()` | `_agent_gate_check()` in `_slm_translate()` | — (no tools) |
| `searcher` | multi_agent_demo | — | — (no SLM path) | `_before_tool_cb` + `GovernedToolkit.gate_check()` |

To instrument additional agents, add both layers: runner-level CB check at the agent
dispatch site (for every execution path, including local model paths), and `GovernedToolkit`
wrapping any tool calls.

---

## Reference

- SDK source: `instrumentation/governed_toolkit.py`
- Gate client: `instrumentation/gate_client.py`
- Gate check backend: `governance_service/gate.py`
- Demo runner (full pattern): `examples/opt-demo/runner.py`
- Demo agent definitions: `examples/opt-demo/agents/orchestrator.py`
- Enforcement page: `eval_ui/pages/11_Governance_Enforcement.py`
- Last updated: 2026-05-27
