# AI Multi-Agent Evaluation, Observability & Governance Platform

A self-hosted platform for **evaluating, observing, and governing any multi-agent AI system** in real time and offline. No external SaaS dependencies — everything runs locally via Docker.

Built on: **ClickHouse · Jaeger · OpenTelemetry · FastAPI · Streamlit · Claude**

<img width="1583" height="891" alt="Screenshot 2026-05-30 at 4 22 09 PM" src="https://github.com/user-attachments/assets/9678cf88-189a-465d-a0d3-e4b6735f3570" />


---

## Table of Contents

1. [What This Platform Does](#1-what-this-platform-does)
2. [Architecture Overview](#2-architecture-overview)
3. [Quick Start](#3-quick-start)
4. [Project Structure](#4-project-structure)
5. [Infrastructure Stack](#5-infrastructure-stack)
6. [Evaluation System](#6-evaluation-system)
7. [Observability System](#7-observability-system)
8. [Instrumentation SDK](#8-instrumentation-sdk)
9. [Eval UI](#9-eval-ui)
10. [AI Governance System](#10-ai-governance-system)
11. [Governance Enforcement](#11-governance-enforcement)
12. [EvalGov Intelligence Agent](#12-evalgov-intelligence-agent)
13. [Multi-Agent Demo](#13-multi-agent-demo)
14. [Running Offline Benchmarks](#14-running-offline-benchmarks)
15. [Onboarding a New Agent System](#15-onboarding-a-new-agent-system)
16. [Evaluation Metrics Reference](#16-evaluation-metrics-reference)
17. [Configuration Reference](#17-configuration-reference)
18. [Common Commands](#18-common-commands)

---

## 1. What This Platform Does

### Observability
- Captures every trace, log, and metric from any agent system via OpenTelemetry
- LLM spans follow OTel GenAI semantic conventions (`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `gen_ai.request.model`)
- Stores everything in ClickHouse for fast analytical queries
- Jaeger for interactive distributed trace exploration

### Evaluation
- **Two modes**: real-time (production monitoring) and offline (benchmark test harness)
- **Three-tier evaluator cascade**: deterministic → LLM-as-judge → multi-turn judge
- **LLM judges**: faithfulness, relevance, instruction following, handoff fidelity, hallucination, safety, QA correctness, custom rubric
- **Multi-turn judges**: context retention, topic adherence, response consistency, conversation relevancy
- **OTel-computed per-trace metrics**: task success, latency, cost, token count, tool success rate, handoff success rate, dead span rate — written to `eval_scores` on every trace
- **Threshold alerting**: 38 of 68 metrics configurable; violations feed shows prompt, trace, agent, and judge reasoning
- **Regression detection**: every run compared against a pinned baseline

### AI Governance
- **13 detection categories**: Safety, Identity, Reliability, Behavior, Lifecycle, Regulatory, Supply Chain, Tool Whitelist, SLO, Incidents, Anomaly Detection, Compliance Scorecards, Risk Register
- **Pre-execution enforcement**: gate check API classifies every agent action by risk tier; high-risk actions paused for human approval (HITL)
- **Circuit breaker**: CLOSED/OPEN/HALF_OPEN state machine per agent
- **Trust scoring**: composite score per agent from identity, behavior, compliance, and network signals
- **Content quality gates**: post-hoc watcher fires every 30s; `flag → hold → block` decision ladder

### EvalGov Intelligence Agent
- **Conversational interface**: Claude Sonnet agent with 44 real-time tools — answer any governance/eval/cost/trace question in plain English
- **On-demand RCA**: ask the agent why a circuit breaker opened, what caused a trust score drop, or what a safety event means — it correlates signals across governance, traces, and eval scores to give a specific root cause and recommended action
- **Live system state**: UI panel auto-refreshes every 60s showing active HITL requests, open incidents, circuit breaker states, and policy violations
- **MCP server**: expose all 44 tools to Claude Code and other AI agents

### Framework Support

| Framework | Support |
|---|---|
| LangChain / LangGraph | ✅ Adapter included |
| AutoGen | ✅ Adapter included |
| CrewAI | ✅ Adapter included |
| Anthropic Claude SDK | ✅ Adapter included |
| OpenAI Agents SDK | ✅ Adapter included |
| OpenAI SDK (direct) | ✅ Auto-instrumented via OpenLLMetry |
| Google ADK | ✅ Works via manual span wrappers (see demo) |
| Custom Python agents | ✅ Manual span context managers |

---

## 2. Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Your Agent System (any SDK)                                    │
│  pip install -e ./instrumentation[langchain]                    │
│  init_telemetry() + framework adapter or manual spans           │
│  GovernedToolkit — gate check + HITL polling on every call      │
└────────────────────────────┬────────────────────────────────────┘
                             │ OTLP gRPC :4317 / HTTP :4318
                             ▼
              ┌──────────────────────────────┐
              │   OTel Collector  :4317/4318  │
              │   Normalises + fans out       │
              └──────┬──────────┬────────────┘
                     │          │
              ┌──────▼──┐  ┌────▼────┐
              │ClickHouse│  │ Jaeger  │
              │:8123/9000│  │:16686   │
              │traces    │  │trace UI │
              │eval_scores│ └─────────┘
              │gov_*     │
              └──────┬───┘
                     │
              ┌──────▼───────────┐    ┌──────────────────────┐
              │  Eval Runner     │    │  Governance Service  │
              │  :8000           │    │  :8002               │
              │  Tiered evals    │    │  Gate check API      │
              │  LLM judges      │    │  Circuit breakers    │
              │  Regression      │    │  Trust scores        │
              └──────────────────┘    │  13 detection cats   │
                                      └──────────┬───────────┘
              ┌──────────────────────────────────┐│
              │  EvalGov Agent  :8003            ││
              │  Chat agent · MCP server         ◄┘
              │  Live system state panel         │
              └──────────────────────────────────┘

              ┌──────────────────────────────────┐
              │  Eval UI  :8501 Streamlit        │
              │  Eval · Traces · Benchmarks      │
              │  AI Governance (13 categories)   │
              │  Governance Enforcement          │
              │  EvalGov Agent (chat + system state) │
              └──────────────────────────────────┘
```

### Data Flow

```
Agent runs → OTel spans → Collector → ClickHouse (stored)
                                    → Jaeger (trace UI)
                                    → Eval Runner → scores → ClickHouse

           → gate check → Governance Service → HITL queue → agent waits
                                             → CB update
                                             → trust score update

ClickHouse + Governance Service → EvalGov Agent → live system state panel
```

---

## 3. Quick Start

### Prerequisites
- Docker Desktop with Compose v2
- Python 3.10+
- Anthropic API key (for LLM judge evaluators and EvalGov Agent)

### 1. Clone and configure

```bash
git clone <your-repo>
cd opt-aieval
cp .env.example .env
# Set ANTHROPIC_API_KEY in .env
```

### 2. Start the platform

```bash
docker compose up -d
# Starts 7 services: ClickHouse, Jaeger, OTel Collector,
#                    Eval Runner, Governance Service, EvalGov Agent, Eval UI
```

### 3. Open the UIs

| UI | URL |
|---|---|
| Eval UI (main dashboard) | http://localhost:8501 |
| Jaeger (trace viewer) | http://localhost:16686 |
| Governance API docs | http://localhost:8002/docs |
| EvalGov Agent API docs | http://localhost:8003/docs |
| ClickHouse console | http://localhost:8123/play |

### 4. Run the demo (optional)

```bash
# Terminal 1 — eval platform is already running from step 2 above (repo root)

# Terminal 2 — demo agent (from the demo directory)
cd examples/opt-demo
cp .env.example .env   # set OPENAI_API_KEY or ANTHROPIC_API_KEY
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8080 --workers 1

# Terminal 3 — chat UI (same demo directory)
cd examples/opt-demo
streamlit run chat_ui.py   # → http://localhost:8502
```

---

## 4. Project Structure

```
opt-aieval/
├── README.md                        This file — platform overview
├── docker-compose.yml               7-service stack
├── .env.example                     Environment variable template
│
├── docs/                            Reference documentation
│   ├── instrumentation-guide.md     Full span schema, attribute reference, evaluator tables
│   ├── gov-instrument-guide.md      Phase 2 gate decision flow, HITL patterns, CB guide
│   ├── eval-metrics-dashboard-guide.md  Filter behaviour + Option B span-level fix guide
│   ├── evalgov-agent.md             EvalGov Agent — tool reference, chat agent, MCP server
│   ├── governance-enforcement-architecture.md  Three-layer enforcement design
│   ├── hitl-and-gate-ui-reference.md  HITL and gate check UI reference
│   └── packaging-recommendations-v2.md  SDK packaging status and v1 roadmap
│
├── infra/
│   ├── clickhouse/
│   │   ├── init.sql                 Database schema (eval + governance tables)
│   │   └── users.xml                ClickHouse user config
│   └── otelcol/
│       └── config.yaml              OTel Collector: receivers, processors, exporters
│
├── instrumentation/                 Instrumentation SDK — install and use this
│   ├── README.md                    ← Start here to instrument your agent system
│   ├── pyproject.toml               pip-installable package config
│   ├── telemetry.py                 OTel init — init_telemetry(), get_tracer(), get_run_id()
│   ├── spans.py                     Canonical span context managers
│   ├── governed_toolkit.py          GovernedToolkit — pre-execution gate check wrapper
│   ├── gate_client.py               Low-level gate check HTTP client
│   ├── normalizer.py                SDK attribute → canonical schema mapping
│   └── adapters/
│       ├── langchain_adapter.py     LangChainEvalAdapter
│       ├── autogen_adapter.py       AutoGenEvalAdapter
│       ├── crewai_adapter.py        CrewAIEvalAdapter
│       ├── claude_adapter.py        ClaudeEvalAdapter
│       └── openai_agents_adapter.py OpenAIAgentsEvalAdapter
│
├── eval_runner/                     Evaluation service (FastAPI :8000)
│   ├── main.py                      REST endpoints + OTLP ingestion + eval dedup
│   ├── config/evaluator_config.yaml Evaluator routing config + regression thresholds
│   ├── evaluators/
│   │   ├── deterministic/           format_check, tool_accuracy, step_efficiency, etc.
│   │   └── llm_judges/              faithfulness, relevance, hallucination, safety, etc.
│   ├── pipeline/
│   │   ├── eval_pipeline.py         Eval orchestration + OTel-computed metrics
│   │   └── conversation_eval.py     Multi-turn judge runner
│   └── db/                          ClickHouse client + repository
│
├── eval_ui/                         Streamlit evaluation UI (:8501)
│   ├── app.py                       Home page with live metrics
│   └── pages/
│       ├── 1_Eval_Testing.py        Run management + baseline
│       ├── 2_Eval_Measurements.py   Traces, Scores, Regression, Benchmarks, Conversations
│       ├── 10_AI_Governance.py      13 governance categories, policy engine, compliance
│       ├── 11_Governance_Enforcement.py  Circuit breakers, HITL approvals, quality gates
│       └── 14_EvalGov_Agent.py      Chat UI + live system state panel
│
├── governance_service/              Governance FastAPI service (:8002)
│   ├── main.py                      90+ REST endpoints across all governance domains
│   ├── db.py                        ClickHouse client + all governance CRUD
│   ├── gate.py                      Pre-execution gate check (risk tier logic)
│   ├── enforcement_engine.py        Trust score + rogue assessment computation
│   ├── quality_gate_checker.py      Post-hoc quality watcher (30s loop)
│   ├── incident_manager.py          Incident creation, dedup, lifecycle
│   └── anomaly_detector.py          Statistical anomaly detection vs baselines
│
├── evalgov_agent/                   EvalGov Intelligence Agent (:8003)
│   ├── agent.py                     Claude chat agent with 44-tool loop
│   ├── tools.py                     Tool implementations + Anthropic schemas
│   ├── mcp_server.py                MCP SSE server exposing all 44 tools
│   └── db.py                        ClickHouse client for analytics queries
│
│
├── benchmarks/                      Offline benchmark test harness
│   ├── runner.py                    CLI: run suites, compare baseline
│   └── suites/
│       ├── unit/cases.json
│       ├── integration/cases.json
│       └── collaboration/cases.json
│
└── examples/
    ├── opt-demo/                    Primary demo — 4-agent system (orchestrator, searcher,
    │                                summarizer, translator) with full governance integration
    ├── langchain_agent_example.py   LangChain agent with LangChainEvalAdapter
    ├── claude_agent_example.py      Claude SDK agent with ClaudeEvalAdapter
    └── custom_agent_example.py      Custom Python agent — all span types demonstrated
```

---

## 5. Infrastructure Stack

### ClickHouse — Primary Data Store

All observability signals and evaluation scores land here.

**OTel tables** (auto-created by collector):
- `otel.otel_traces` — all spans
- `otel.otel_logs` — structured logs
- `otel.otel_metrics_*` — metrics

**Eval tables** (created by `infra/clickhouse/init.sql`):
- `otel.eval_runs` — groups of traces (one per benchmark run or tagged production period)
- `otel.eval_scores` — evaluator scores linked to trace_id; `eval_type` ∈ `deterministic` / `llm_judge` / `multiturn_judge` / `otel_computed`
- `otel.benchmarks` — offline test cases with `dataset_version` for stable regression comparisons
- `otel.prompt_evals` — per-agent prompt/response pairs with token counts and score maps; deduplicated by `(trace_id, span_id)`
- `otel.alert_thresholds` — one row per metric; operator + threshold value + enabled flag
- `otel.human_reviews` — human feedback on eval scores

**Direct query access:**

```sql
-- Recent eval scores
SELECT trace_id, metric, score, reasoning
FROM otel.eval_scores
ORDER BY evaluated_at DESC LIMIT 20;

-- Average score per metric for a run
SELECT metric, avg(score)
FROM otel.eval_scores
WHERE run_id = 'your-run-id'
GROUP BY metric;
```

### OTel Collector

Receives OTLP from agents, normalises SDK-specific attributes, fans out to backends.

- `:4317` — OTLP gRPC
- `:4318` — OTLP HTTP
- `:8888` — Collector self-metrics
- `:13133` — Health check

Normalisation maps LangChain / AutoGen / CrewAI attribute names to the canonical schema before storage. Config: `infra/otelcol/config.yaml`.

### Jaeger

Interactive distributed trace explorer at http://localhost:16686. Every agent execution shows as a full span tree with parent-child nesting.

---

## 6. Evaluation System

### Two Modes

**Offline (test harness)** — run before deploying changes:
```bash
python benchmarks/runner.py --suite unit --name "my-run-v2"
python benchmarks/runner.py --suite unit --name "my-run-v2" --compare-baseline
```

**Online (real-time)** — monitors production traffic automatically. Every `agent.task` span completion triggers the eval pipeline. LLM judges run on a 15% sample to control cost.

### Evaluator Tiers

| Tier | Evaluators | Cost | When it runs |
|---|---|---|---|
| **Deterministic** | format_compliance, tool_accuracy, step_efficiency, tool_output_handling, tool_error_rate, timeout_rate | Free | Always, 100% |
| **LLM Judge** | faithfulness, relevance, instruction_following, handoff_fidelity, hallucination_score, safety_score, qa_correctness, custom_rubric | ~$0.001/trace | Offline: 100% · Online: 15% sample |
| **Multi-turn** | context_retention, topic_adherence, response_consistency, conversation_relevancy | ~$0.001/conversation | Per conversation_id after idle 60s |
| **OTel-computed** | task_success, agent_failure, timeout, task_latency_ms, cost_usd, token_count, context_window_utilization, tool_call_success_rate, handoff_success_rate, dead_span_rate | Free | Always, 100% |

### Eval Runner API

```
GET  /health
GET  /runs                         List all eval runs
POST /runs                         Create a new run
PUT  /runs/{run_id}/baseline       Set run as baseline
POST /runs/{run_id}/evaluate       Trigger offline eval
GET  /runs/{run_id}/scores         Aggregated scores
GET  /runs/{run_id}/regression     Score deltas vs baseline
GET  /traces/{trace_id}/scores     All scores for a trace
```

### Configuring Evaluators

Edit `eval_runner/config/evaluator_config.yaml`:

```yaml
sampling:
  online_llm_judge_rate: 0.15     # 15% of traces get LLM judges
  always_judge_on_failure: true   # always judge failed tasks

regression:
  alert_threshold: 0.05           # flag if metric drops >5%
```

---

## 7. Observability System

### What Gets Captured Automatically

Once `init_telemetry()` is called, OpenLLMetry auto-instrumentation captures every LLM call:

- Model name — `gen_ai.request.model`
- Token counts — `gen_ai.usage.input_tokens` / `gen_ai.usage.output_tokens`
- Latency — span `Duration`
- Finish reason — `gen_ai.response.finish_reasons`

### Canonical Span Schema

```
agent.task:
  agent.id, agent.role, task.id, task.input, task.output,
  task.status, run.id,
  trace.source (production|benchmark|exploratory),
  conversation.id

agent.tool_call:
  agent.id, tool.name, tool.input (JSON), tool.output (JSON), tool.success, run.id

agent.handoff:
  from.agent_id, to.agent_id, handoff.reason, handoff.context (JSON)

agent.memory:
  agent.id, memory.op (read|write), memory.key, memory.scope

agent.decision:
  agent.id, decision.input, decision.options, decision.chosen, decision.reason
```

### Trace → Eval Pipeline

When a span with `SpanName = 'agent.task'` arrives:

1. OTel Collector forwards span to Eval Runner (`/v1/traces`)
2. Eval Runner assembles the full trace tree from ClickHouse
3. Evaluator pipeline fires (deterministic → LLM judges per sampling config)
4. Scores written to `otel.eval_scores`

---

## 8. Instrumentation SDK

The `instrumentation/` directory is a pip-installable SDK. It provides `init_telemetry()`, framework adapters for all supported SDKs, canonical span context managers, and `GovernedToolkit` for pre-execution governance enforcement.

**To instrument your agent system, start here:**

```
instrumentation/README.md
```

That document covers installation, framework adapters, span context managers, governance enforcement integration, and how to expose a `/chat` endpoint for benchmark execution.

The detailed reference material lives in:
- [`docs/instrumentation-guide.md`](docs/instrumentation-guide.md) — full span schema, attribute reference, evaluator tables, multi-agent context patterns
- [`docs/gov-instrument-guide.md`](docs/gov-instrument-guide.md) — gate decision flow, HITL patterns, circuit breaker integration

---

## 9. Eval UI

The Streamlit UI at **http://localhost:8501** provides the main evaluation and governance interface.

### Pages

| Page | Purpose |
|---|---|
| **Home** | Live metrics: total runs, traces evaluated, avg scores |
| **1 · Eval Testing** | Create runs, set baseline, trigger offline eval |
| **2 · Eval Measurements** | Traces, Scores, Regression, Benchmarks, Conversations, Eval Metrics (68 metrics / 9 categories), Prompt Analysis |
| **10 · AI Governance** | 13 governance categories, policy engine, anomaly events, compliance reports |
| **11 · Governance Enforcement** | Circuit breakers, trust scores, HITL approvals, quality gates, on-demand enforcement cycle |
| **14 · EvalGov Agent** | Chat UI + live system state panel |

### Key Features

- **Estimated cost** computed from `gen_ai.usage.*` tokens × configurable rates from `gov_threshold_config`
- **Source filter** in Eval Measurements — narrows to `production`, `benchmark`, or `exploratory` traces
- **Conversation filter** — filter by partial or full `conversation.id` across Traces and Prompt Analysis
- **Threshold alerting** on 38 of 68 metrics; violations feed shows full prompt, trace, agent, and judge reasoning
- **Regression comparison** — select two runs for side-by-side metric deltas and radar chart

For filter behaviour details and known limitations, see [`docs/eval-metrics-dashboard-guide.md`](docs/eval-metrics-dashboard-guide.md).

---

## 10. AI Governance System

The governance service runs as a separate FastAPI service (`:8002`) and wraps every agent action with a policy and enforcement layer.

### 13 Detection Categories

| Category | What it tracks |
|---|---|
| **Safety** | Prompt injection, jailbreak attempts, toxicity, bias |
| **Identity** | Session token TTL violations, over-provisioned tool access |
| **Reliability** | Error rates, SLO compliance, uptime |
| **Behavior** | Output consistency, persona adherence, out-of-distribution outputs |
| **Lifecycle** | Deployment mode (canary/stable), version change log |
| **Regulatory** | Compliance scorecards (GDPR/SOC2/HIPAA/ISO27001), risk register |
| **Supply Chain** | Model artifact hash verification, supply chain registry |
| **Tool Whitelist** | Allowed/denied tool list per agent |
| **SLO Config** | Error budget tracking, burn rate |
| **Incidents** | 8 trigger types, dedup logic, MTTD/MTTC/MTTR KPIs |
| **Anomaly Detection** | Z-score anomaly detection vs computed baselines |
| **Compliance Scorecards** | Automated scoring across regulatory frameworks |
| **Risk Register** | Risk items with owner, severity, mitigation, review date |

### Pre-Execution Enforcement (Gate Check)

When a governed agent calls `/gate/check` before any action, the gate applies:

1. Circuit breaker state check — OPEN → block immediately
2. PII escalation — if payload contains PII, escalate risk tier
3. Budget check — if token budget exhausted, escalate risk tier

Risk tier outcome:
- `low` → allow
- `medium` → allow (logged)
- `high` → HITL pause (agent polls until approved/rejected)
- `critical` → block

Pre-execution enforcement is enabled/disabled via `enforcement.phase2_enabled` in governance thresholds.

### Circuit Breaker

Each agent has a circuit breaker with states `CLOSED → OPEN → HALF_OPEN → CLOSED`:

- Auto-opens when failure threshold is breached (default: 5 failures)
- When OPEN, all gate check calls return a block response
- After `recovery_timeout_minutes`, transitions to HALF_OPEN
- Manual controls: reset (close), half-open (probe), quarantine (permanent open)

### Governance Service API

```
GET  /governance/summary
POST /gate/check
GET  /enforcement/circuit-breakers
POST /enforcement/circuit-breakers/{role}/reset
GET  /enforcement/trust-scores
GET  /hitl/queue
PUT  /hitl/{request_id}           Approve or reject
GET  /incidents
GET  /compliance/report
```

---

## 11. Governance Enforcement

The Enforcement page (page 11) is the operational console for real-time agent control.

### Tab 1 — Agent Pre-Execution Enforcement

- Trust scores per agent (composite + component breakdown)
- Burn rates (SLO error budget velocity)
- Rogue assessment scores (frequency, entropy, capability, quarantine flag)
- Circuit breaker states with manual controls
- Pre-execution gate toggle and on-demand enforcement cycle

### Tab 2 — Content Quality Enforcement

Post-hoc quality gate — watcher fires every 30 seconds, reads eval scores, compares against per-agent thresholds:

| Decision | Effect |
|---|---|
| `flag` | Log only |
| `hold` | Creates a HITL queue entry; next invocation is gated pending human review |
| `block` | Escalates gate tier to critical — all subsequent actions blocked |

### Tab 3 — HITL Approvals

Pending queue shows agent, action type, risk tier, escalation reasons, full context. Approve/Reject with reviewer notes. Both the Policy Engine tab (page 10) and Enforcement HITL tab (page 11) read/write the same `otel.gov_hitl_queue` table.

See [`docs/hitl-and-gate-ui-reference.md`](docs/hitl-and-gate-ui-reference.md) for full reference.

---

## 12. EvalGov Intelligence Agent

The EvalGov Agent (`:8003`) is the intelligence and interaction layer on top of the governance system.

### Three Capabilities

**Chat Agent** — Claude Sonnet with 44 real-time tools:

```
"What's wrong right now?"
"Why did the circuit breaker open for agent searcher?"
"Show me what it cost to run yesterday"
"Approve the pending HITL for request abc123"
"Which agents have trust scores below 0.5?"
```

**MCP** — expose all tools to Claude Code and other AI agents:

```bash
claude mcp add evalgov http://localhost:8003/mcp/sse
```

### Chat UI (page 14)

Two-panel layout: chat (left) + live system state (right). The right panel shows active HITL requests, open incidents, circuit breaker states, and policy violations — auto-refreshes every 60s.

See [`docs/evalgov-agent.md`](docs/evalgov-agent.md) for the complete tool reference and chat agent details.

---

## 13. Multi-Agent Demo

The primary demo is **`examples/opt-demo`** — a 4-agent system with Google ADK + full governance integration.

### Agents

| Agent | Role | Tool |
|---|---|---|
| **Orchestrator** | Classifies query intent, routes or responds directly | — |
| **Searcher** | Searches web, synthesises answer with citations | DuckDuckGo |
| **Summarizer** | Summarises text in exact word count | — |
| **Translator** | Translates to any target language | — |

### Setup

```bash
# Terminal 1 — eval platform (repo root: opt-aieval/)
docker compose up -d

# Terminal 2 — demo agent (opt-aieval/examples/opt-demo/)
cd examples/opt-demo
cp .env.example .env   # set OPENAI_API_KEY or ANTHROPIC_API_KEY
pip install -r requirements.txt
uvicorn api:app --host 0.0.0.0 --port 8080 --workers 1

# Terminal 3 — chat UI (opt-aieval/examples/opt-demo/)
cd examples/opt-demo
streamlit run chat_ui.py   # → http://localhost:8502
```

> Use `--workers 1`. Multiple workers use separate memory spaces and bypass the in-memory eval dedup dict, causing duplicate `prompt_eval` rows.

### Multi-Turn Conversation Tracking

Every chat session is assigned a `conversation_id` (UUID) propagated to all agent spans — orchestrator and sub-agents. This enables the conversation browser in Eval UI, multi-turn LLM judges, and conversation-level filtering in Prompt Analysis.

### Instrumentation in the Demo

| Event | Span | Where |
|---|---|---|
| Root task | `agent.task` (orchestrator) | `runner.py run_agent()` |
| Sub-agent call | `agent.task` (searcher / summarizer / translator) | `runner.py _call_agent()` |
| `conversation.id` | Set on all `agent.task` spans | root + all child spans |
| web_search | `agent.tool_call` | `_maybe_emit_tool_span()` |
| LLM calls | `openai.chat`, `call_llm` | Google ADK + LiteLLM auto-instrumentation |

---

## 14. Running Offline Benchmarks

```bash
# Run unit suite
python benchmarks/runner.py --suite unit --name "baseline-v1" --agent-version "v1.0"

# Run all suites
python benchmarks/runner.py --suite all --name "pre-deploy-v2"

# Compare against baseline
python benchmarks/runner.py --suite unit --name "post-change-v2" --compare-baseline
```

### Benchmark Suites

| Suite | Cases | Focus |
|---|---|---|
| `unit` | 5 | Single-turn: factual QA, format, instruction following |
| `integration` | 4 | Multi-step: research, tool use, reasoning chains |
| `collaboration` | 3 | Multi-agent: orchestration, parallel agents |

### Adding Test Cases

Edit `benchmarks/suites/{suite}/cases.json` or use the Eval UI (Benchmarks tab):

```json
{
  "suite": "unit",
  "name": "My test case",
  "task_input": "The prompt to send to the agent",
  "expected_output": "Optional ground truth for exact-match eval",
  "rubric": {"criteria": ["criterion 1", "criterion 2"]},
  "difficulty": "medium"
}
```

---

## 15. Onboarding a New Agent System

**Full instructions are in [`instrumentation/README.md`](instrumentation/README.md).** The short version:

### 1. Install the SDK

```bash
pip install -e ./instrumentation[all]   # or pick your framework extra
```

### 2. Initialize telemetry

```python
from instrumentation import init_telemetry
init_telemetry(service_name="my-agent")
```

### 3. Attach an adapter or wrap spans manually

```python
# LangChain
from instrumentation import LangChainEvalAdapter
adapter = LangChainEvalAdapter(agent_id="lc-1", agent_role="researcher")
agent.invoke(input, config={"callbacks": [adapter]})

# Or manually (any framework)
from instrumentation import agent_task
with agent_task(agent_id="a1", role="researcher", task_input=query) as task:
    result = my_agent.run(query)
    task.set_output(result)
```

### 4. Verify in Jaeger

Open http://localhost:16686, select your `service.name`, confirm `agent.task` spans appear with correct parent-child nesting.

### 5. Add governance enforcement (optional)

```python
from instrumentation import GovernedToolkit
toolkit = GovernedToolkit(agent_role="searcher")
governed_search = toolkit.wrap("web_search", web_search)
```

See [`docs/gov-instrument-guide.md`](docs/gov-instrument-guide.md) for Phase 2 gate patterns and the full checklist.

---

## 16. Evaluation Metrics Reference

### LLM Judge Scores

| Metric | What it measures |
|---|---|
| `faithfulness` | Output grounded in source context — no hallucinations |
| `relevance` | Output addresses the actual question |
| `instruction_following` | Output follows explicit prompt constraints |
| `hallucination_score` | Fabricated facts not in tool outputs or context |
| `safety_score` | 6-dimension safety: toxic, harmful, PII, bias, unsafe advice, role violation |
| `qa_correctness` | Answer correctness vs expected output (benchmark mode) |
| `custom_rubric` | Configurable rubric per benchmark case |
| `handoff_fidelity` | Context preserved across agent handoffs |

### Multi-Turn Judges

| Metric | What it measures |
|---|---|
| `context_retention` | Each turn correctly references prior conversation context |
| `topic_adherence` | Conversation stays on-topic across turns |
| `response_consistency` | No contradictions between turns |
| `conversation_relevancy` | Each response relevant to the original user intent |

### OTel-Computed Per-Trace Metrics

| Metric | Source | Unit |
|---|---|---|
| `task_success` | Root `agent.task` StatusCode | 0/1 |
| `agent_failure` | Root `agent.task` StatusCode | 0/1 |
| `task_latency_ms` | Root `agent.task` Duration | ms |
| `cost_usd` | `gen_ai.usage.*` tokens × governance rates | USD |
| `token_count` | All LLM spans `gen_ai.usage.*` | tokens |
| `context_window_utilization` | Max `gen_ai.usage.input_tokens` / 128k | 0–1 |
| `tool_call_success_rate` | `agent.tool_call` StatusCode | 0–1 |
| `tool_calls_per_task` | Count of `agent.tool_call` spans | count |
| `handoff_success_rate` | `agent.handoff` StatusCode | 0–1 |
| `dead_span_rate` | Orphaned spans / total spans | 0–1 |

### Score Interpretation

| Score | Meaning |
|---|---|
| 0.8 – 1.0 | Good — meets expectations |
| 0.5 – 0.8 | Marginal — review recommended |
| 0.0 – 0.5 | Poor — action needed |

### Eval Metrics Dashboard (page 2 → Eval Metrics tab)

68 metrics across 9 categories. Metrics marked "Needs instrumentation" require additional span attributes not yet emitted.

| Category | Total | Live |
|---|---|---|
| 1 · Task completion & accuracy | 10 | 8 |
| 2 · Efficiency & cost | 7 | 6 |
| 3 · Multi-agent coordination | 8 | 5 |
| 4 · Reasoning & decision quality | 7 | 2 |
| 5 · Tool & MCP integration | 10 | 6 |
| 6 · Reliability & safety | 8 | 6 |
| 7 · Memory & context management | 6 | 1 |
| 8 · Observability & tracing | 7 | 5 |
| 9 · Conversation quality | 4 | 4 |
| **Total** | **68** | **43** |

See [`docs/eval-metrics-dashboard-guide.md`](docs/eval-metrics-dashboard-guide.md) for filter behaviour, known limitations, and the span-level filter fix guide.

---

## 17. Configuration Reference

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required** — LLM judges and EvalGov Agent |
| `OPENAI_API_KEY` | — | Required for demo agents using OpenAI |
| `JUDGE_MODEL` | `anthropic/claude-haiku-4-5-20251001` | Model used by LLM judges |
| `AGENT_MODEL` | `anthropic/claude-sonnet-4-6` | Model used by EvalGov chat agent |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` | OTel Collector endpoint |
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `CLICKHOUSE_PORT` | `9000` | ClickHouse native port |
| `GOVERNANCE_SERVICE_URL` | `http://localhost:8002` | Governance service URL |
| `EVALGOV_AGENT_URL` | `http://localhost:8003` | EvalGov agent URL |
| `HITL_TIMEOUT_MINUTES` | `15` | Minutes before HITL timeout triggers a finding |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint (if using local models) |

### Key Config Files

| File | Purpose |
|---|---|
| `infra/otelcol/config.yaml` | OTel Collector pipelines, exporters, normalisation rules |
| `eval_runner/config/evaluator_config.yaml` | Which evaluators run, LLM sampling rate, regression threshold |
| `infra/clickhouse/init.sql` | Database schema — all tables and materialized views |

---

## 18. Common Commands

```bash
# Platform lifecycle
docker compose up -d          # Start all 7 services
docker compose down           # Stop all services
docker compose logs -f        # Tail all service logs
docker compose ps             # Show service health

# Rebuild after code changes (Python services only)
docker compose build eval-runner && docker compose up -d eval-runner
docker compose build evalgov-agent && docker compose up -d evalgov-agent

# ClickHouse SQL console
docker exec -it aieval-clickhouse clickhouse-client

# Benchmark runner
python benchmarks/runner.py --suite unit --name "my-run"
python benchmarks/runner.py --suite all --compare-baseline

# Connect Claude Code to EvalGov tools via MCP
claude mcp add evalgov http://localhost:8003/mcp/sse
```
