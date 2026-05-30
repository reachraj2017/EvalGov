# Governance Enforcement Architecture: Bridging Observability to Enforcement

Strategic assessment of evolving aieval from a post-hoc governance system into a full
pre-execution enforcement platform, and how it compares to the Microsoft Agent Governance Toolkit
approach. Assessed April 2026.

---

## The Core Architectural Difference

aieval and the MSFT toolkit take fundamentally different approaches to governance:

| | aieval (current) | MSFT toolkit |
|---|---|---|
| **Pattern** | Sidecar watcher — reads OTel spans after execution | Middleware wrapper — intercepts actions before execution |
| **Timing** | Post-hoc, per trace (~30 second polling cycle) | Pre-execution, per action (<0.1 ms in-process overhead) |
| **Agent awareness** | Agents are passive — they emit spans, governance reads them | Agents are active participants — middleware sits in their call path |
| **Enforcement model** | Detect and report | Detect and prevent |

To move aieval to enforcement, the architecture needs to support all three phases:

```
Before agent acts  →  During agent acts  →  After agent acts
  (enforcement)         (detection)           (evaluation)

  MSFT covers:          Both cover:           aieval covers:
  gate check            OTel spans            LLM judges
  circuit breakers      safety scanning       benchmarks
  trust scoring         PII detection         regression
  policy decision       anomaly detection     multi-turn eval
```

---

## Can We Replicate MSFT Capabilities?

**~80% fully replicable, ~20% partially or not at all.**

### Fully buildable (on existing signals and infrastructure)

All 10 items in the priority build list are implementable within our existing architecture —
they are aggregations and extensions of signals we already collect:

| Capability | Build approach |
|---|---|
| Agent trust score | Aggregate existing events (safety, policy, anomaly, identity) into rolling per-agent score |
| Circuit breakers | Add `suspended` status per agent_role; reliability tracker triggers on failure threshold |
| Rogue agent detection | Add tool-call frequency + entropy tracking to identity guard |
| Kill switch | UI quarantine button + policy engine checks suspension status |
| Burn rate alerts | Multi-window (1h, 6h, 24h) error rate computation vs SLO budget |
| MCP tool fingerprinting | Hash tool definitions on first observation; alert on change |
| AI Bill of Materials | Extend supply chain registry schema for provenance chains, training data, weights |
| Content quality gates | Apply threshold enforcement to existing LLM judge scores |
| Progressive delivery automation | Rollout spec engine that consumes `deployment.mode` span attributes |
| Contextual policy engine | Extend gate check API to accept and evaluate execution context |

### Partially replicable

| Capability | What we can do | What we can't fully match |
|---|---|---|
| **Enforcement latency** | Gate check round-trip ~2–5 ms | MSFT in-process middleware is <0.1 ms; irrelevant for LLM agents (calls are 200–2000 ms) but real for high-frequency tool loops |
| **Capability delegation** | Tool whitelist per agent_role | MSFT has parent→child delegation chains where capabilities can only narrow, never expand |
| **Credential lifecycle** | API keys with manual revocation | MSFT has 15-minute TTL tokens with automatic rotation and <5 second revocation propagation |
| **Compliance attestation** | Unsigned JSON compliance reports | MSFT signs reports with Ed25519 for third-party verifiability |

### Not replicable without major infrastructure investment

| Capability | Why |
|---|---|
| **Cryptographic agent identity (DID/Ed25519)** | Requires PKI infrastructure; decentralized identifiers need key management layer |
| **Merkle-chained audit logs** | Technically implementable in ClickHouse but hash-chain computation on every write is complex and expensive |
| **Multi-language SDKs** | Python-first is feasible; .NET/TypeScript/Rust/Go agents would use the gate check REST API directly |
| **Kernel-level execution rings** | Application-layer ring enforcement is achievable; true hardware ring separation requires OS/container isolation (MSFT notes the same limitation) |

---

## How Instrumentation Changes

### Today — agents are passive

Agents emit OTel spans and governance observes them asynchronously. Governance has no ability
to prevent an action from happening — only to detect and report after it occurs.

```
Agent code                    Governance service
──────────                    ──────────────────
span.start("agent.task")
  tool_result = web_search()  ←── (no awareness)
span.end()
     │
     │ OTel spans (async, ~30s delay)
     ▼
Governance watcher
  → safety check
  → policy check          (too late to prevent anything)
  → incident created
```

### With enforcement — agents become active participants

Two changes to agent code, neither of which touches OTel span structure:

**Change 1: Gate check becomes mandatory before tool invocations**

```python
# Today — agent calls tool directly
result = web_search(query)

# With enforcement — agent asks governance first
decision = governance.check(
    action="tool_call",
    tool="web_search",
    payload={"query": query},
)
if decision == "block":
    raise PolicyViolation("web_search blocked by governance policy")
result = web_search(query)
```

The gate check is synchronous — the agent waits for the decision before proceeding.
The governance service evaluates: trust score, circuit breaker state, policy rules,
tool whitelist, and execution context. Response is immediate (<5ms).

**Change 2: A thin agent-side SDK wraps tool calls transparently**

Rather than agents manually calling the HTTP endpoint before every tool, a lightweight
Python wrapper handles it without changing agent logic:

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
#   1. gate check → allow/block/require_approval
#   2. if allow: call web_search, capture result
#   3. emit gate.decision as span attribute
#   4. report outcome back to governance (updates trust score, circuit breaker state)
```

**What does NOT change:**
- OTel span structure (`agent.task`, `agent.tool_call`, `agent.handoff` spans — unchanged)
- LLM auto-instrumentation (OpenAIInstrumentor — unchanged)
- Eval pipeline (LLM judges, benchmarks, regression — unchanged)
- Jaeger, ClickHouse — unchanged
- All existing governance tabs and metrics — unchanged

The SDK is an addition alongside existing instrumentation, not a replacement.

### New span attributes added by the SDK

The SDK stamps these attributes on existing spans — no new span types needed:

```
gate.decision        = "allow" | "block" | "require_approval"
gate.policy_matched  = "pii_leak_rate_block" | ...   (which rule fired)
gate.trust_score     = 0–1000                         (agent trust at decision time)
gate.circuit_state   = "closed" | "open" | "half_open"
gate.request_id      = <uuid>                         (links span to HITL queue entry)
```

---

## The Enforcement Architecture

With the SDK in place, governance operates across all three phases simultaneously:

```
┌─────────────────────────────────────────────────────────────────────┐
│  BEFORE EXECUTION                                                     │
│                                                                       │
│  Agent calls tools.web_search()                                       │
│       │                                                               │
│       ▼                                                               │
│  GovernedToolkit.check()  ──► governance-service:8002/gate/check      │
│                                    │                                  │
│                                    ├── Trust score ≥ threshold?       │
│                                    ├── Circuit breaker CLOSED?        │
│                                    ├── Tool in whitelist?             │
│                                    ├── Policy rules pass?             │
│                                    ├── Agent suspended?               │
│                                    └── Context allows this action?    │
│                                         │                             │
│                              allow / block / require_approval         │
│                                         │                             │
│       ◄─────────────────────────────────┘                             │
│       │                                                               │
│  If allow: execute tool                                               │
│  If block: raise PolicyViolation (tool never called)                  │
│  If require_approval: pause + route to HITL queue                    │
└─────────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  DURING EXECUTION                                                     │
│                                                                       │
│  OTel spans flow via collector → ClickHouse                           │
│  Governance watcher polls every 30 seconds:                           │
│    → Safety guard (injection, jailbreak, toxic, bias)                 │
│    → PII detection (scan task.input + task.output)                    │
│    → Anomaly detection (Z-score vs baselines)                         │
│    → Rogue detection (tool frequency + entropy)                       │
│    → Trust score update (new signals incorporated)                    │
│    → Circuit breaker state update (error rate check)                  │
└─────────────────────────────────────────────────────────────────────┘
                    │
                    ▼
┌─────────────────────────────────────────────────────────────────────┐
│  AFTER EXECUTION                                                      │
│                                                                       │
│  LLM judges evaluate completed trace:                                 │
│    → Hallucination, faithfulness, relevance, safety score             │
│    → Content quality gates (accuracy, tone, format compliance)        │
│    → Multi-turn: conversation completeness, knowledge retention       │
│                                                                       │
│  Governance metrics computed:                                         │
│    → Reliability: error rate, p95/p99, availability, error budget     │
│    → Burn rate: multi-window budget consumption velocity              │
│    → Incidents: auto-create P1/P2 with dedup and MTTD/MTTC/MTTR      │
│                                                                       │
│  Audit trail written:                                                 │
│    → Every decision, every signal, every metric snapshot              │
│    → Compliance scorecard updated                                     │
│    → Regression and benchmark data available                          │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Current State vs Target State

### Maturity by layer

```
                        TODAY           AFTER BUILD
                        ─────           ───────────

Observability           ████████████    ████████████   (unchanged — already strong)
(OTel, Jaeger,
ClickHouse, Jaeger)

Eval                    ████████████    ████████████   (unchanged — already strong)
(LLM judges,
benchmarks, regression)

Governance              █████████░░░    ████████████   (+ trust score, rogue detection,
(13 categories,                                         burn rates, MCP fingerprinting,
detection, incidents)                                   AI-BOM, quality gates)

Enforcement             ██░░░░░░░░░░    █████████░░░   (+ mandatory gate check SDK,
(gate check,                                            circuit breakers, kill switch,
circuit breakers)                                       contextual policy)
```

### What "enforcement" changes in practice

| Scenario | Today (detect + report) | With enforcement (detect + prevent) |
|---|---|---|
| Jailbreak attempt | Safety event written, P1 incident created | Gate check blocks the tool call before it executes |
| Agent uses unregistered model | Identity event written, supply chain violation logged | Gate check blocks the LLM call if model not in registry |
| Error rate spikes (SLA breach) | P2 incident created, MTTC clock starts | Circuit breaker opens — all further calls rejected until manually reset |
| Rogue agent detected | Anomaly event written, high-severity incident | Kill switch auto-quarantine triggered by rogue detection score |
| Budget exhausted | Policy BLOCK decision logged | Gate check blocks all new tool calls until budget resets |
| Tool not in whitelist | Least-privilege violation logged | Gate check blocks the tool call (never reaches the tool) |

---

## Does This Achieve End-to-End Governance?

**Yes — substantially — with one honest dependency.**

### What "end-to-end" delivers

The combination of enforcement + detection + evaluation creates a closed loop that no
other open system currently provides:

- **Prevention**: Gate check stops violations before they happen
- **Detection**: Real-time OTel analysis catches what slips through or emerges during execution
- **Evaluation**: LLM judges and benchmark regression validate quality and catch drift over time
- **Response**: Circuit breakers, kill switch, and incidents close the loop automatically

This is stronger than MSFT's approach, which only covers prevention. MSFT has no LLM judge
evaluation, no benchmark regression, and no multi-turn conversation analysis.

### The critical dependency

End-to-end enforcement only works if agents integrate the SDK or call the gate check.

An agent that does not call the gate check is still:
- Observable (OTel spans flow normally)
- Evaluable (LLM judges run on every completed trace)
- Governable (incidents are created, safety events written, metrics computed)

But it is **not enforceable** — we can detect the violation but cannot prevent the action.

MSFT has the same limitation. Their framework adapters (for LangChain, LangGraph, CrewAI,
AutoGen, etc.) solve this by making the gate check zero-friction — developers using those
frameworks get enforcement automatically. Our equivalent is the `GovernedToolkit` SDK wrapper.

**The SDK is therefore as important as the server-side capabilities.** Without it, the
enforcement layer exists but adoption requires explicit developer effort for every tool call.

---

## The Unique Position of the Combined System

After the build, aieval occupies a position no other system currently holds:

| System | Observability | Eval / Quality | Governance | Enforcement |
|---|---|---|---|---|
| **aieval (after build)** | ✅ OTel-native, ClickHouse, Jaeger | ✅ LLM judges, benchmarks, regression | ✅ 13 categories, trust score, incidents | ✅ Gate check SDK, circuit breakers, kill switch |
| **MSFT toolkit** | Partial (OTLP export) | ❌ No LLM judges | ✅ Policy, identity, audit | ✅ In-process middleware (faster) |
| **LangSmith / Langfuse** | ✅ Traces and prompts | Partial (human review) | ❌ | ❌ |
| **Arize / Whylogs** | ✅ ML observability | Partial | ❌ | ❌ |
| **Guardrails AI / NeMo** | ❌ | ❌ | Partial (output filtering) | Partial (output only) |

The combination of all four layers in a single deployable system — with a shared data store
(ClickHouse), shared identity (agent_role), and shared configuration (threshold system) —
is the architectural moat.

---

## Build Sequence

Recommended order based on dependencies and incremental value delivery:

### Phase 1 — Automated response (server-side, no SDK needed)
1. **Agent trust score** — rolling composite from existing event tables
2. **Burn rate alerts** — multi-window error budget computation
3. **Rogue agent detection** — tool frequency + entropy scoring
4. **Circuit breaker + kill switch** — suspension state + policy enforcement

*Value: governance becomes self-responding. Incidents trigger automated containment
without waiting for human action.*

### Phase 2 — Pre-execution enforcement (requires agent SDK)
5. **GovernedToolkit SDK** — thin Python wrapper; gate check + circuit breaker signal handling
6. **Contextual policy engine** — context (env, user, time) in gate check evaluation
7. **Content quality gates** — apply thresholds to existing LLM judge scores
8. **MCP tool fingerprinting** — hash tool definitions; detect rug pulls

*Value: shift from "detect and report" to "detect and prevent." Violations stopped
before execution.*

### Phase 3 — Provenance and delivery (longer-horizon)
9. **AI-BOM expansion** — provenance chains, training data cards, weights hashes
10. **Progressive delivery automation** — canary rollout spec engine + auto-rollback

*Value: supply chain integrity and deployment safety at scale.*

---

## Reference

- MSFT toolkit source: `/Users/rajlearn/MSFT-gov-kit/agent-governance-toolkit`
- Capability comparison: `/Users/rajlearn/aieval/docs/governance-capability-comparison.md`
- aieval governance source: `/Users/rajlearn/aieval/governance_service/`
- aieval instrumentation guide: `/Users/rajlearn/aieval/docs/instrumentation-guide.md`
- Assessment date: 2026-04-30
