# EvalGov Intelligence Agent

_Module documentation. Last updated: 2026-05-26. Tools: 44._

---

## What It Is

The EvalGov Intelligence Agent is the intelligence and interaction layer on top of the AI Governance system. It does three things:

1. **Proactive monitoring** — continuously watches all governance signals (circuit breakers, HITL timeouts, rogue agents, incidents, trust scores, safety violations, policy blocks, metric anomalies, behavior violations) and uses Claude to generate root cause analysis and recommendations for each anomaly it detects. Findings are stored, displayed in the UI, and persist across sessions.

2. **Conversational interface** — a Claude-backed agent with direct access to 44 real-time tools covering every part of the governance and eval system. Operators ask questions in plain English; the agent fetches real data, correlates signals, and can take actions (approve HITL, reset circuit breakers, resolve incidents, bulk-clear findings). Can retrieve actual prompt text, agent responses, eval scores with reasoning, benchmark definitions, safety events, policy decisions, configuration thresholds, and more.

3. **External access** — an MCP server for Claude Code and other AI agents, and a CLI for terminal operators and machine-to-machine use.

It is a separate FastAPI service (`evalgov-agent`, port 8003) that wraps the existing `governance-service` REST API and ClickHouse — no new data collection. All signals were already being gathered; this layer adds intelligence and interaction.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    EvalGov Agent Service (:8003)                  │
│                                                                    │
│  ┌────────────────┐  ┌──────────────────┐  ┌──────────────────┐  │
│  │ Proactive       │  │  Chat Agent      │  │   MCP Server     │  │
│  │ Monitor         │  │  (Claude Sonnet) │  │  (/mcp/sse)      │  │
│  │ (60s poll loop) │  │  44 tools        │  │  44 tools        │  │
│  └───────┬─────────┘  └──────┬───────────┘  └──────┬───────────┘  │
│          │                   │                      │               │
│  ┌───────▼───────────────────▼──────────────────────▼────────────┐ │
│  │                     Tool Layer (tools.py)                      │ │
│  └──────────────────────┬────────────────────┬────────────────────┘ │
└─────────────────────────┼────────────────────┼──────────────────────┘
                          │                    │
              ┌───────────▼──────┐   ┌─────────▼──────────────────┐
              │ governance-svc   │   │ ClickHouse (:9000)          │
              │ (:8002) REST API │   │ otel.prompt_evals           │
              │ HITL, CB, trust, │   │ otel.otel_traces            │
              │ incidents, etc.  │   │ otel.eval_scores            │
              └──────────────────┘   │ otel.eval_runs              │
                                     │ otel.benchmarks             │
                                     │ otel.gov_policy_decisions   │
                                     │ otel.gov_routing_decisions  │
                                     │ otel.gov_agent_findings     │
                                     └─────────────────────────────┘
```

---

## Service Startup

The agent, proactive monitor, and MCP server all start automatically as part of a single process — nothing requires a separate launch command.

When the `evalgov-agent` container starts (via `docker compose up`), the following happens in order:

```python
# evalgov_agent/main.py

# 1. MCP server is mounted at import time — before the app even starts serving
try:
    from mcp_server import build_mcp_app
    app.mount("/mcp", build_mcp_app())   # /mcp/sse and /mcp/messages/ wired in
except Exception as exc:
    log.warning("mcp_server_unavailable", reason=str(exc))  # graceful degradation

# 2. FastAPI lifespan runs on first request / startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    db = AgentDB()
    db.ensure_tables()           # creates gov_agent_findings if it doesn't exist

    monitor = ProactiveMonitor(db)
    monitor.start()              # launches 60s asyncio background poll loop
    app.state.monitor = monitor

    yield                        # service is now live

    monitor.stop()               # clean shutdown on container stop
```

After startup all four capabilities are live simultaneously on port 8003:

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Chat agent — user prompts via Streamlit UI |
| `GET /findings`, `POST /findings/{id}/acknowledge`, etc. | Findings CRUD — UI findings panel |
| `GET /health` | Health check — used by Docker healthcheck and load balancers |
| `GET /mcp/sse`, `POST /mcp/messages/` | MCP SSE server — Claude Code and external agents |

**The MCP server has one safety net:** it is wrapped in `try/except` so if the `mcp` package is missing, the rest of the service starts cleanly without it. In the current Docker image `mcp` is in `requirements.txt`, so it always mounts successfully.

**The proactive monitor runs as an `asyncio.Task`** on the same event loop as the FastAPI server — no separate thread or process. It does not block request handling.

---

## How a Chat Turn Works (End-to-End)

This section traces the exact path from user prompt to response. **MCP is not involved in the chat UI path** — it is a separate entry point documented below.

```
1. User types in browser
         │
         ▼
2. Streamlit  (14_EvalGov_Agent.py)
     - appends message to st.session_state.messages
     - builds history_for_api = all prior turns [{role, content}, ...]
     - POST /chat  {message, history}  →  evalgov-agent:8003
         │
         ▼
3. FastAPI main.py  POST /chat
     - calls agent.chat(message, history)
         │
         ▼
4. agent.py  chat()
     - builds messages = history + [{"role": "user", "content": message}]
     - calls LiteLLM  litellm.completion()
       with: model, system_prompt, TOOL_SCHEMAS (44 schemas), messages
         │
         ▼
5. Claude (claude-sonnet-4-6)
     - reads the 44 tool schemas in the request
     - selects which tools to call to answer the question
     - returns stop_reason="tool_use" with tool_use blocks
         │
         ▼
6. agent.py  tool-use loop  (up to 10 rounds)
     for each tool_use block:
       execute_tool(name, inputs)  →  tools.py TOOL_MAP
             │
             ├── governance enforcement tools
             │     _gov("/enforcement/trust-scores")
             │     _gov_put("/hitl/{id}", {...})
             │     → HTTP GET/POST/PUT to governance-service:8002
             │     → governance-service queries ClickHouse, applies business logic
             │
             └── eval / trace / prompt / config tools
                   get_db().get_prompt_detail(trace_id)
                   get_db().search_prompts(...)
                   get_db().get_cost_breakdown(...)
                   → ClickHouse TCP connection on port 9000 (direct, no HTTP hop)
         │
     appends tool results to messages
     calls Claude again with results
     repeats until stop_reason="end_turn"
         │
         ▼
7. FastAPI returns  {response, tool_calls: [{name, inputs, result_preview}]}
         │
         ▼
8. Streamlit renders
     - assistant message with full response text
     - expandable "🔧 N tool(s) called" disclosure per message
     - appends to st.session_state.messages (context for next turn)
```

### Session context

The chat is **stateless server-side**. The Streamlit client owns the conversation history in `st.session_state.messages` and sends the full history with every POST. This means:

- Context accumulates across turns for the life of the browser session
- No server-side session management — the service is horizontally scalable
- "Clear Chat" button wipes `st.session_state.messages`, starting a fresh context
- If the browser tab is closed, history is gone (intentional — no cross-session persistence)

---

## Two Data Paths Inside the Tool Layer

Tools in `tools.py` use two different backends depending on whether business logic is needed:

### Path 1 — Governance-service REST API

Used for all enforcement and state-management tools. The governance-service owns these data models with business logic (state machines, dedup, cascade effects). Going through the REST API ensures that logic is applied consistently regardless of who is calling.

```
Tool function → _gov("/path")  →  governance-service:8002  →  ClickHouse
                _gov_put(...)
                _gov_post(...)
```

Tools that use this path: `get_system_health`, `get_hitl_queue`, `approve_hitl`, `reject_hitl`, `bulk_approve_hitl`, `get_circuit_breakers`, `reset_circuit_breaker`, `quarantine_agent`, `get_trust_scores`, `get_rogue_assessments`, `get_incidents`, `resolve_incident`, `bulk_resolve_incidents`, `get_anomalies`, `get_burn_rates`, `get_quality_gate_decisions`, `get_policy_violations`, `get_safety_events`, `get_thresholds`, `get_agent_budgets`, `get_version_pins`, `get_lifecycle_changes`, `get_compliance_scorecard`, `get_compliance_report`, `get_risk_register`, `get_model_registry`, `get_reliability_summary`

### Path 2 — Direct ClickHouse (AgentDB)

Used for eval content, trace analytics, and proactive monitor findings. These are read-only analytics queries with no business logic — direct ClickHouse is faster and avoids an unnecessary HTTP hop.

```
Tool function → get_db()._run(sql)  →  ClickHouse:9000  (TCP, native driver)
```

Tools that use this path: `get_recent_traces`, `get_agent_performance`, `get_cost_breakdown`, `get_error_rates`, `get_prompt_detail`, `search_prompts`, `get_eval_runs`, `get_benchmarks`, `get_eval_scores_detail`, `get_policy_decisions`, `get_routing_decisions`, `get_findings`, `acknowledge_finding`, `resolve_finding`, `bulk_resolve_findings`, `bulk_acknowledge_findings`

---

## Chat UI vs MCP: Two Separate Entry Points, Same Tool Layer

The same 44 tools are accessible through two completely independent paths:

| | Chat UI | MCP / Claude Code / External Agent |
|---|---|---|
| **Entry point** | `POST /chat` → `agent.py` | `GET /mcp/sse` → `mcp_server.py` |
| **Who decides which tools to call** | Claude (via LiteLLM in `agent.py`) | The external MCP client |
| **Session / context** | Client sends full history each turn | No session — each tool call is independent |
| **Tool execution** | `execute_tool()` in `tools.py` | Same `execute_tool()` |
| **Data sources** | governance-service:8002 + ClickHouse:9000 | Same |
| **Use case** | Human operators via chat UI | Claude Code CLI, other AI agents, automation |

The MCP server is a **Starlette ASGI app mounted at `/mcp`** on the same FastAPI process. It does not go through `agent.py` or the Anthropic API — it simply receives a tool call over SSE and routes it to `execute_tool()`. There is no Claude invocation on the MCP path unless the calling client (e.g., Claude Code) uses Claude to decide what to call.

### What the tool layer wraps

**Via governance-service REST API:**
HITL queue, circuit breakers, trust scores, rogue assessments, incidents, quality gate decisions, anomaly events, burn rates, policy violations, safety events, thresholds, agent budgets, version pins, lifecycle changes, compliance scorecards, risk register, model registry, compliance report, reliability summary

**Via direct ClickHouse query:**
Agent traces, eval scores by metric, cost breakdown by agent, error rates, prompt text + response by trace ID, recent prompt search, eval runs, benchmark definitions, eval scores with reasoning, policy decisions, model routing decisions, proactive monitor findings

---

## Component 1: Proactive Monitor

### How it works

The monitor runs as a background `asyncio.Task` started at service startup. Every 60 seconds (configurable via `MONITOR_POLL_SECONDS`) it runs one detection cycle:

1. **Fetch signals** from governance-service and ClickHouse:
   - All circuit breaker states
   - All pending HITL requests with their `created_at` timestamps
   - All rogue assessments with `quarantine_recommended`
   - All open incidents with severity `p0` or `p1`
   - All trust scores
   - Safety events in the last 1 hour (`detected = true`)
   - Policy hard-block decisions in the last 1 hour (direct ClickHouse query)
   - Statistical anomaly events with z-score ≥ 3 in the last 1 hour
   - Behavior violations (scope, policy, capability abuse) in the last 1 hour

2. **Detect anomalies** — checks each signal against a threshold:

   | Anomaly type | Trigger condition | Source | Default severity |
   |---|---|---|---|
   | `circuit_breaker_open` | Any CB in OPEN state | current state | critical |
   | `hitl_timeout` | HITL request pending > 15 min (`HITL_TIMEOUT_MINUTES`) | current pending | high |
   | `rogue_agent_detected` | `quarantine_recommended = true` | current state | critical |
   | `critical_incident` | Open incident with severity `p0` or `p1` | current open | critical / high |
   | `low_trust_score` | Trust score < 0.4 | current state | high |
   | `safety_violation` | Any `detected = true` safety event in last 1h | `/safety/events` | high |
   | `policy_block` | Any `decision = 'block'` in last 1h | `gov_policy_decisions` | high |
   | `metric_anomaly` | Any metric with z-score ≥ 3 in last 1h | `/anomalies` | medium |
   | `behavior_violation` | scope/policy/capability violation in last 1h | `/behavior/events` | high |

   Safety, policy, anomaly, and behavior detectors group events by `agent_role` — one finding per agent, not one per event.

3. **Deduplication (state-based)** — before creating a new finding, checks `gov_agent_findings` for any existing `active` or `acknowledged` finding of the same `(finding_type, affected_agent)`, with no time constraint. Skips if one exists.

   This means a persistent condition (e.g. a circuit breaker that has been OPEN for days) generates exactly **one finding** that stays visible until an operator resolves it — not a new finding every poll cycle. A new finding is only raised when the previous one has been resolved and the condition re-occurs.

   > **Why this matters:** The original implementation used a 30-minute time window for dedup. Because governance signals like circuit breakers and low trust scores represent continuous state rather than discrete events, a persistent condition would generate a new finding every 30 minutes (48 per day, 336 per week). The Live Findings panel would fill with duplicate rows for the same underlying issue. State-based dedup fixes this.

4. **Generate RCA** — calls Claude Sonnet via LiteLLM (`generate_rca()` in `agent.py`) with a structured JSON summary of the signal data. This is a **separate, one-shot LiteLLM call** — not the chat agent, no tool use loop, no conversation history. The system prompt instructs Claude to return only a JSON object with:
   - `severity` — `critical | high | medium | low` (may override the default)
   - `summary` — 2-sentence plain-English description
   - `rca` — 2-sentence root cause analysis
   - `recommendation` — one specific, actionable next step

5. **Store finding** — inserts into `otel.gov_agent_findings` (ReplacingMergeTree):
   ```
   finding_id, finding_type, severity, title, summary, rca, recommendation,
   signal_data (JSON), affected_agent, status, acknowledged_by, created_at, updated_at
   ```

**Important:** The RCA is generated **once per finding** at detection time and persisted. By the time an operator opens the findings panel, asks the chat agent about it, or the CLI lists findings, the RCA text is already written in ClickHouse — the UI and agent are just reading rows. There is no on-demand LLM call triggered by viewing a finding.

### Findings lifecycle

```
Monitor detects anomaly
        │
        ▼
 INSERT → status: 'active'
        │
        ├── Operator clicks Acknowledge in UI / CLI
        │         └── INSERT new row, status: 'acknowledged'
        │
        └── Operator clicks Resolve / underlying issue fixed
                  └── INSERT new row, status: 'resolved'
```

The `gov_agent_findings` table uses `ReplacingMergeTree(updated_at)` — status transitions write new rows; the `FINAL` keyword surfaces the latest version.

---

## Component 2: Chat Agent

### Interface

The chat API is **stateless** — the Streamlit UI sends the full conversation history with every request. The server does not store chat sessions. This keeps the service horizontally scalable and avoids session management.

```
POST /chat
Body: { "message": "...", "history": [{role, content}, ...] }
Returns: { "response": "...", "tool_calls": [{name, inputs, result}, ...] }
```

### Tool use loop

The agent uses LiteLLM with tool use (routed to Claude Sonnet via `anthropic/` prefix). Each chat turn:

1. Sends the full conversation history + user message + all 44 tool schemas to Claude Sonnet
2. If the model returns `stop_reason: "tool_use"`, executes each requested tool, appends results, calls again
3. Repeats up to 10 tool-call rounds per turn (handles complex multi-source queries)
4. Returns the final text response and a list of all tools called with their inputs and truncated results (for the expandable disclosure in the UI)

### System prompt direction

The agent is instructed to:
- Always call tools before answering — never guess numbers
- Proactively correlate signals (e.g., if a CB is open, also check the agent's trust score and recent incidents)
- Give specific answers: exact numbers, agent names, timestamps, recommended action
- Take actions when asked (approve HITL, reset CB, resolve incident) and confirm what was done

### Example interactions

| Question | Tools called |
|---|---|
| "What's wrong right now?" | `get_system_health` → drill into open CBs, pending HITL, rogue assessments |
| "Show agent analyzer's performance" | `get_agent_performance`, `get_trust_scores(agent=analyzer)`, `get_circuit_breakers(agent=analyzer)` |
| "Why did the CB open for searcher?" | `get_circuit_breakers(searcher)`, `get_incidents(agent=searcher)`, `get_recent_traces(searcher)` |
| "Approve the HITL for request abc123" | `approve_hitl(request_id=abc123)` → confirms action |
| "Approve all pending HITL requests" | `bulk_approve_hitl()` → approves all in one round |
| "Resolve all open incidents" | `bulk_resolve_incidents()` → resolves all in one round |
| "Resolve all findings" | `bulk_resolve_findings()` → resolves entire active backlog in one round |
| "What did production agents cost last 30 days?" | `get_cost_breakdown(hours=720, source=production)` |
| "What was the actual user prompt for trace abc?" | `get_prompt_detail(trace_id=abc)` → returns prompt_text, response_text, model, tokens |
| "Show me recent prompts from the orchestrator" | `search_prompts(agent_name=orchestrator, hours=720)` |
| "List our eval test runs this month" | `get_eval_runs(hours=720)` |
| "What benchmark test cases do we have?" | `get_benchmarks(suite=unit)` |
| "Why did the faithfulness score drop?" | `get_eval_scores_detail(metric=faithfulness)` → shows reasoning text |
| "What content triggered safety rules?" | `get_safety_events(hours=48)` → shows matched_text |
| "What are the current budget thresholds?" | `get_thresholds(category=budget)` |
| "What are token budgets for each agent?" | `get_agent_budgets()` |
| "Which model versions are pinned?" | `get_version_pins()` |
| "What changed in config this week?" | `get_lifecycle_changes(hours=168)` |
| "What's our SOC2 compliance score?" | `get_compliance_scorecard(framework=SOC2)` |
| "Which agents have trust scores below 0.5?" | `get_trust_scores()` → filters and reports |
| "Are there any unresolved incidents?" | `get_incidents(status=open)` |

---

## Component 3: MCP Server

The MCP server runs as a Starlette ASGI app mounted at `/mcp` on the same FastAPI service.

### Connecting Claude Code

The MCP server is a **local SSE server** — it runs inside the `evalgov-agent` container and is port-mapped to `localhost:8003`. Connect from any MCP client running on the host machine:

```bash
claude mcp add evalgov --transport sse http://localhost:8003/mcp/sse
```

After adding, any Claude Code session can call EvalGov tools directly. Examples:

```
"are there any open incidents in evalgov?"
"approve HITL request <id> in evalgov with reviewer=ops"
"show me all circuit breakers in evalgov"
```

### Exposed tools (44 total)

All 44 tools are available to both the Chat UI (via Claude's tool use) and MCP clients.

**Governance enforcement** (via governance-service REST)

| Tool | Description |
|---|---|
| `get_system_health` | Full real-time health snapshot — HITL, CBs, incidents, trust, rogue |
| `get_hitl_queue` | List HITL requests by status/window |
| `approve_hitl` | Approve a single pending HITL request by `request_id` (unblocks the agent) |
| `reject_hitl` | Reject a single pending HITL request by `request_id` |
| `bulk_approve_hitl` | Approve **all** pending HITL requests in one call |
| `get_circuit_breakers` | CB states (CLOSED/OPEN/HALF_OPEN) for all or one agent |
| `reset_circuit_breaker` | Manually close a CB |
| `quarantine_agent` | Manually open CB with quarantine flag |
| `get_trust_scores` | Trust scores for all agents or history for one |
| `get_rogue_assessments` | Rogue detection: frequency, entropy, capability, composite scores |
| `get_incidents` | Incidents by status / time window |
| `resolve_incident` | Mark a single incident as resolved by `incident_id` |
| `bulk_resolve_incidents` | Resolve **all** open incidents in one call |
| `get_anomalies` | Statistical anomaly events vs baselines |
| `get_burn_rates` | SLO error budget burn rates |
| `get_quality_gate_decisions` | Quality gate flag/hold/block decisions |
| `get_gate_audit_log` | Gate check summary |
| `get_policy_violations` | Policy engine violation summary |
| `get_safety_events` | Safety rule violations including matched_text |
| `get_thresholds` | All 34 configurable governance thresholds with current values |
| `get_agent_budgets` | Token budgets, cost limits, and today's usage per agent |
| `get_version_pins` | Model version pin configuration per agent |
| `get_lifecycle_changes` | Configuration change audit log |
| `get_compliance_scorecard` | Compliance scores by regulatory framework |
| `get_risk_register` | Risk register: open/mitigated/accepted items |
| `get_model_registry` | Approved model whitelist |
| `get_compliance_report` | Generate full compliance summary |
| `get_reliability_summary` | Reliability KPIs per agent |

**Eval & trace analytics** (direct ClickHouse)

| Tool | Description |
|---|---|
| `get_recent_traces` | Recent `agent.task` OTel spans with role/duration/status. Default 30-day window. |
| `get_agent_performance` | Eval scores aggregated by metric (faithfulness, relevance, coherence, etc.) |
| `get_cost_breakdown` | Token usage + USD cost per agent from `otel.prompt_evals`. Filterable by source (production/benchmark/exploratory). Rates from governance config. |
| `get_error_rates` | Error rate per agent on `agent.task` spans |
| `get_prompt_detail` | Full prompt_text + response_text + scores for a specific trace_id |
| `search_prompts` | Browse recent prompt/response pairs; filter by agent, source, time window |
| `get_eval_runs` | List eval test runs (suite, baseline flag, agent version) |
| `get_benchmarks` | Benchmark test case definitions (task_input, expected_output, rubric) |
| `get_eval_scores_detail` | Eval scores with full reasoning text for a trace or run |
| `get_policy_decisions` | Policy engine verdicts (warn/block/pass) with value vs threshold |
| `get_routing_decisions` | Model routing decisions: tier, model chosen, estimated cost savings |

**Proactive monitor findings** (direct ClickHouse → `gov_agent_findings`)

| Tool | Description |
|---|---|
| `get_findings` | Monitor findings filtered by severity/status/window. Defaults to all-time so the agent sees the full backlog, not just the last 24h. |
| `acknowledge_finding` | Acknowledge a single finding by `finding_id` |
| `resolve_finding` | Resolve (close) a single finding by `finding_id` |
| `bulk_acknowledge_findings` | Acknowledge **all** active findings in one call |
| `bulk_resolve_findings` | Resolve **all** active and acknowledged findings in one call |

---

## Component 4: EvalGov CLI

### Installation

```bash
pip install typer rich httpx
# Set env vars (or they default to localhost):
export EVALGOV_AGENT_URL=http://localhost:8003
export GOVERNANCE_SERVICE_URL=http://localhost:8002
```

### Commands

```bash
# System
evalgov health                              # Check agent + governance service health

# Natural language
evalgov ask "what's wrong right now?"       # Ask the agent anything
evalgov ask "why is analyzer CB open?"

# HITL management
evalgov hitl list                           # Pending HITL requests (default)
evalgov hitl list --status approved         # Filter by status
evalgov hitl approve <request_id>           # Approve (unblocks agent)
evalgov hitl reject  <request_id> --notes "reason"

# Incidents
evalgov incidents list                      # Open incidents
evalgov incidents list --status resolved
evalgov incidents resolve <incident_id>

# Circuit breakers
evalgov cb list                             # All CB states
evalgov cb list --agent searcher            # Filter by agent
evalgov cb reset searcher                   # Manually close CB
evalgov cb quarantine searcher --reason "Anomalous behavior"

# Agent status
evalgov agents                              # Trust score + CB state for all agents
evalgov trust                               # Trust score breakdown
evalgov trust --agent analyzer
evalgov rogue                               # Rogue detection assessments

# Findings
evalgov findings list                       # Active findings
evalgov findings list --severity critical
evalgov findings ack <finding_id>           # Acknowledge
evalgov findings ack <finding_id> --by ops-team

# Observability
evalgov traces                              # Recent traces (last 6h)
evalgov traces --agent searcher --hours 24
evalgov costs                               # Cost breakdown (last 24h)
evalgov scores                              # Eval scores by metric

# All commands support --json for machine-readable output
evalgov hitl list --json
evalgov findings list --json | jq '.[] | select(.severity == "critical")'
```

---

## Chat UI Layout

The Streamlit page (`14_EvalGov_Agent.py`) uses a two-panel layout:

```
┌──────────────────────────────────────┬────────────────────────────┐
│  Chat (60%)                          │  Live Findings (40%)       │
│                                      │                            │
│  [hint text on empty state]          │  🔴 2  🟠 1  🟡 0  🔵 0   │
│                                      │  ──────────────────────    │
│  user: what's wrong?                 │  🔴 CB Open: analyzer      │
│  agent: [response with tool          │  Agent analyzer CB is...   │
│          call disclosure]            │  ▼ RCA & Recommendation    │
│                                      │    Root cause: ...         │
│  user: approve hitl abc123           │    💡 Rec: Reset CB after  │
│  agent: Approved — agent will        │       confirming tool fix  │
│         proceed with search action   │  [Acknowledge] [Resolve]   │
│                                      │  ───────────────────────   │
│  [Ask anything about your AI         │  🟠 HITL Timeout: searcher │
│   systems...]                        │  ...                       │
│                                      │  [Window: 24h] [↺]         │
└──────────────────────────────────────┴────────────────────────────┘
```

**Chat panel features:**
- Clear Chat button resets session history
- Each assistant message shows an expandable "N tools called" disclosure with tool name, inputs, and result preview
- History is stateless (client-managed) — survives page reload by Streamlit session state

**Findings panel features:**
- Severity count badges (🔴🟠🟡🔵) summarise active findings
- Each finding shows: title, timestamp, affected agent, 2-sentence summary
- Expandable "RCA & Recommendation" section per finding
- Acknowledge / Resolve buttons per finding; Acknowledge All for bulk
- Window selector (1h–7d) for how far back to show findings
- Separate "acknowledged/resolved" expander for history

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required** — used for chat agent and RCA generation |
| `GOVERNANCE_SERVICE_URL` | `http://localhost:8002` | Internal governance-service URL |
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `CLICKHOUSE_PORT` | `9000` | ClickHouse native port |
| `CLICKHOUSE_DB` | `otel` | Database name |
| `AGENT_MODEL` | `claude-sonnet-4-6` | Claude model for chat + RCA |
| `MONITOR_POLL_SECONDS` | `60` | Proactive monitor poll interval |
| `HITL_TIMEOUT_MINUTES` | `15` | Minutes before HITL timeout becomes a finding |
| `EVALGOV_AGENT_URL` | `http://localhost:8003` | Used by eval-ui and CLI |

---

## ClickHouse Table: `otel.gov_agent_findings`

Created by the agent service on startup. Uses `ReplacingMergeTree(updated_at)` — status transitions are written as new rows; always query with `FINAL`.

```sql
CREATE TABLE IF NOT EXISTS otel.gov_agent_findings (
    finding_id      String,
    finding_type    String,   -- 'circuit_breaker_open' | 'hitl_timeout' | 'rogue_agent_detected' | 'critical_incident' | 'low_trust_score'
    severity        String,   -- 'critical' | 'high' | 'medium' | 'low'
    title           String,
    summary         String,   -- 2-sentence description
    rca             String,   -- 2-sentence root cause analysis from Claude
    recommendation  String,   -- actionable next step from Claude
    signal_data     String,   -- JSON blob of raw signal values
    affected_agent  String,
    status          String,   -- 'active' | 'acknowledged' | 'resolved'
    acknowledged_by String,
    created_at      DateTime DEFAULT now(),
    updated_at      DateTime DEFAULT now()
) ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (finding_id)
```

**Useful queries:**

```sql
-- Active critical findings
SELECT title, rca, recommendation, affected_agent, created_at
FROM otel.gov_agent_findings FINAL
WHERE status = 'active' AND severity = 'critical'
ORDER BY created_at DESC;

-- Findings per agent in the last 7 days
SELECT affected_agent, severity, count() AS cnt
FROM otel.gov_agent_findings FINAL
WHERE created_at >= now() - INTERVAL 7 DAY
GROUP BY affected_agent, severity
ORDER BY cnt DESC;
```

---

## Integration with the Broader System

```
Governance tabs (pages 10, 11):
  → Human operators review and act on governance signals
  → Actions write to ClickHouse (HITL decisions, CB state changes)

EvalGov Agent (page 14):
  → Proactive monitor reads the same signals every 60s
  → Generates RCA+recommendations via Claude when anomalies are found
  → Chat agent surfaces the same data conversationally
  → Actions taken via chat/CLI write through governance-service REST API
  → MCP server exposes all tools to Claude Code and other AI agents

Webhook delivery (Phase 6 — pending):
  → gov_webhook_configs table already exists
  → When Phase 6 alerting is built, it will push the same findings
    to Slack/email/webhook; the in-UI findings panel is the current
    equivalent
```

---

## Files

```
evalgov_agent/
├── main.py          FastAPI app, lifespan (start monitor + ensure tables), REST endpoints
│                    Mounts MCP server at /mcp; exposes /chat, /findings, /health
├── agent.py         Claude agent with tool use loop (up to 10 rounds per turn)
│                    generate_rca() for proactive monitor (separate Claude call)
│                    SYSTEM_PROMPT with tool-use guidance and data-source rules
├── tools.py         44 tool implementations grouped by domain
│                    TOOL_MAP dict: name → function (dispatcher)
│                    TOOL_SCHEMAS list: Anthropic JSON schemas for all 44 tools
│                    Two backends: _gov() for REST, get_db() for ClickHouse
│                    Bulk action tools: bulk_approve_hitl, bulk_resolve_incidents,
│                    bulk_resolve_findings, bulk_acknowledge_findings
├── monitor.py       ProactiveMonitor: asyncio background task, 60s poll loop
│                    Detects 9 anomaly types, state-based dedup, calls generate_rca()
│                    Writes findings to otel.gov_agent_findings
├── db.py            AgentDB: ClickHouse TCP client (port 9000)
│                    Findings CRUD (gov_agent_findings, ReplacingMergeTree)
│                    Analytics: prompt_evals, eval_runs, benchmarks, eval_scores
│                    Analytics: gov_policy_decisions, gov_routing_decisions
│                    Token rate lookup from gov_threshold_config
├── mcp_server.py    MCP SSE server (Starlette ASGI, mounted at /mcp)
│                    list_tools() → same TOOL_SCHEMAS
│                    call_tool() → same execute_tool() → same tools.py
├── requirements.txt fastapi, uvicorn, clickhouse-driver, anthropic, mcp, httpx
└── Dockerfile       python:3.11-slim, port 8003

evalgov_cli/
└── main.py          Typer CLI: health, ask, hitl, incidents, cb, agents, trust, rogue,
                     findings, traces, costs, scores

eval_ui/pages/
└── 14_EvalGov_Agent.py   Streamlit chat UI + findings sidebar
```

---

## Issues Fixed (2026-05-26)

### 1. Live Findings panel showing historical duplicates

**Root cause:** The proactive monitor used a 30-minute time-based dedup window (`finding_exists_recently(..., minutes=30)`). Persistent conditions — a circuit breaker that has been OPEN for days, a trust score that has been below threshold for a week — are continuous state, not discrete events. With a 30-minute window, the monitor re-raised the same condition as a fresh finding every 30 minutes (48 per day), flooding the Live Findings panel with identical rows all passing the UI's time filter.

**Fix (`evalgov_agent/db.py`):** Changed `finding_exists_recently()` from time-based to state-based dedup. The query no longer filters on `created_at` — it simply checks whether any `active` or `acknowledged` finding exists for `(finding_type, affected_agent)`:

```python
# Before — re-raised every 30 minutes for persistent conditions
"AND status IN ('active', 'acknowledged') "
f"AND created_at >= now() - INTERVAL {int(minutes)} MINUTE"

# After — one finding per active condition until it is resolved
"AND status IN ('active', 'acknowledged')"
```

A new finding is only created when the condition genuinely re-occurs after the previous finding was resolved.

---

### 2. Agent could not resolve findings — only acknowledge

**Root cause:** `db.resolve_finding()` existed in `db.py` and `POST /findings/{id}/resolve` existed in `main.py`, but no tool was registered in `tools.py`. The agent had no callable tool for resolution. When it said "I've resolved the finding," it was either only acknowledging or failing silently.

**Fix (`evalgov_agent/tools.py`):** Added `resolve_finding(finding_id)` tool, registered it in `TOOL_MAP` and `TOOL_SCHEMAS`.

---

### 3. Agent could not clear a full backlog — 10-round limit

**Root cause:** To resolve N findings the agent had to: call `get_findings` (1 round), then call `acknowledge_finding` or `resolve_finding` once per item (N rounds). With 10 max tool-call rounds per turn, any backlog larger than ~8 items was only partially cleared.

The same problem applied to incidents (`resolve_incident` one-by-one) and HITL requests (`approve_hitl` one-by-one).

**Fix (`evalgov_agent/tools.py`):** Added four bulk action tools:

| Tool | Action |
|---|---|
| `bulk_resolve_findings()` | Resolves all `active` + `acknowledged` findings in one call |
| `bulk_acknowledge_findings()` | Acknowledges all `active` findings in one call |
| `bulk_resolve_incidents()` | Resolves all `open` incidents in one call |
| `bulk_approve_hitl()` | Approves all `pending` HITL requests in one call |

Each bulk tool fetches the full list internally and loops, completing the entire operation within a single tool-call round.

---

### 4. `get_findings` default 24h window hid the backlog from the agent

**Root cause:** `get_findings` defaulted to `hours=24`. Findings older than 24 hours were invisible to the agent even if still `active`. The agent would report "no findings" or only partially see the backlog.

**Fix:** Default changed to `hours=8760` (1 year) with `limit=100`, so the agent always sees the full active backlog regardless of age.

---

## Related Documents

- `docs/llm-call-points.md` — **complete inventory of every LLM API call in the system**, models used, trigger conditions, token profiles, and cost estimates
- `docs/buildfwd-focus.md` — overall roadmap and next phases
- `docs/governance-enforcement-architecture.md` — three-layer enforcement architecture
- `docs/hitl-and-gate-ui-reference.md` — HITL and gate check UI reference
- `docs/agent-lightning-optimization.md` — AL-1/AL-2/AL-3 optimization phases
