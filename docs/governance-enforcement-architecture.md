# Governance Enforcement Architecture

The platform operates governance across three phases simultaneously — before an agent acts,
during execution, and after completion. This document describes how each phase works and
how they connect into a closed enforcement loop.

---

## The Three-Phase Enforcement Model

```
Before agent acts  →  During agent acts  →  After agent acts
  (enforcement)         (detection)           (evaluation)

  gate check            OTel spans            LLM judges
  circuit breakers      safety scanning       benchmarks
  trust scoring         PII detection         regression
  HITL approval         anomaly detection     multi-turn eval
  policy decision       rogue detection       quality gates
```

---

## Before Execution — Gate Check & Enforcement

Every tool call made through `GovernedToolkit` is evaluated by the governance service
before the tool is allowed to run. The gate check is synchronous — the agent waits for
a decision before proceeding.

```
Agent calls tools.web_search()
     │
     ▼
GovernedToolkit.check()  ──► governance-service:8002/gate/check
                                  │
                                  ├── Trust score ≥ threshold?
                                  ├── Circuit breaker CLOSED?
                                  ├── Tool in whitelist?
                                  ├── Policy rules pass?
                                  ├── Agent suspended?
                                  └── Context allows this action?
                                       │
                            allow / block / require_approval
                                       │
     ◄─────────────────────────────────┘
     │
If allow:            execute tool
If block:            raise PolicyViolation (tool never called)
If require_approval: pause + route to HITL queue
```

Response time is under 5ms. The governance service evaluates trust score, circuit breaker
state, tool whitelist, policy rules, and execution context in a single synchronous call.

---

## Agent Integration — GovernedToolkit SDK

Rather than agents manually calling the gate check HTTP endpoint before every tool,
a lightweight Python wrapper handles enforcement transparently:

```python
from aieval.governance import GovernedToolkit

# Initialise once — wraps all tool calls with gate check + outcome reporting
tools = GovernedToolkit(
    agent_role="searcher",
    governance_url="http://governance-service:8002",
    api_key=os.getenv("GOVERNANCE_API_KEY"),
)

# Agent code is unchanged — enforcement happens inside the wrapper
result = tools.web_search(query=query)
# wrapper does:
#   1. gate check → allow / block / require_approval
#   2. if allow: call web_search, capture result
#   3. emit gate.decision as span attribute
#   4. report outcome back to governance (updates trust score, circuit breaker state)
```

**What does not change when adding the SDK:**
- OTel span structure (`agent.task`, `agent.tool_call`, `agent.handoff` spans)
- LLM auto-instrumentation (OpenAIInstrumentor)
- Eval pipeline (LLM judges, benchmarks, regression)
- Jaeger, ClickHouse
- All existing governance tabs and metrics

The SDK is an addition alongside existing instrumentation, not a replacement.

### Span attributes added by the SDK

The SDK stamps these attributes on existing spans — no new span types needed:

```
gate.decision        = "allow" | "block" | "require_approval"
gate.policy_matched  = "pii_leak_rate_block" | ...   (which rule fired)
gate.trust_score     = 0–1000                         (agent trust at decision time)
gate.circuit_state   = "closed" | "open" | "half_open"
gate.request_id      = <uuid>                         (links span to HITL queue entry)
```

---

## During Execution — Detection

While the agent runs, OTel spans flow through the collector into ClickHouse. The
governance watcher polls every 30 seconds and runs:

- **Safety guard** — injection, jailbreak, toxic content, bias detection
- **PII detection** — scans `task.input` and `task.output`
- **Anomaly detection** — Z-score vs rolling baselines per agent
- **Rogue detection** — tool call frequency and entropy scoring
- **Trust score update** — new signals incorporated into composite score
- **Circuit breaker state update** — error rate checked against threshold

---

## After Execution — Evaluation

Once a trace is complete, the eval pipeline runs:

- **LLM judges** — hallucination, faithfulness, relevance, safety, instruction following
- **Content quality gates** — accuracy, tone, format compliance against thresholds
- **Multi-turn judges** — conversation completeness, context retention, knowledge continuity
- **Governance metrics** — reliability (error rate, p95/p99), burn rate (multi-window budget consumption), incidents (auto-created P1/P2 with MTTD/MTTC/MTTR)
- **Audit trail** — every decision, every signal, every metric snapshot written to ClickHouse
- **Compliance scorecard** — updated per agent on each completed trace
- **Regression and benchmark data** — available in Eval UI for trend analysis

---

## Enforcement Scenarios

| Scenario | Enforcement response |
|---|---|
| Jailbreak attempt | Gate check blocks the tool call before it executes |
| Agent uses unregistered model | Gate check blocks the LLM call if model not in registry |
| Error rate spikes past SLA threshold | Circuit breaker opens — all further calls rejected until reset |
| Rogue agent detected | Kill switch quarantine triggered by rogue detection score |
| Budget exhausted | Gate check blocks all new tool calls until budget resets |
| Tool not in whitelist | Gate check blocks the tool call (never reaches the tool) |
| High-risk action requires review | HITL queue — agent pauses, waits for human approval |

---

## The Enforcement Dependency

End-to-end enforcement only works if the agent integrates `GovernedToolkit` or calls
the gate check directly.

An agent that does not call the gate check is still:
- **Observable** — OTel spans flow normally
- **Evaluable** — LLM judges run on every completed trace
- **Governable** — incidents are created, safety events written, metrics computed

But it is **not enforceable** — violations can be detected and reported but not prevented
before the action executes. The `GovernedToolkit` wrapper is therefore the integration
point that completes the enforcement loop.
