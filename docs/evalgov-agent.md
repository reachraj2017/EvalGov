# EvalGov Intelligence Agent

_Module documentation. Last updated: 2026-05-26. Tools: 44._

---

## What It Is

The EvalGov Intelligence Agent is the intelligence and interaction layer on top of the AI Governance system. It does three things:

1. **Conversational interface** — a Claude-backed agent with direct access to 44 real-time tools covering every part of the governance and eval system. Operators ask questions in plain English; the agent fetches real data, correlates signals, and can take actions (approve HITL, reset circuit breakers, resolve incidents). Can retrieve actual prompt text, agent responses, eval scores with reasoning, benchmark definitions, safety events, policy decisions, configuration thresholds, and more.

2. **Live system state panel** — the right side of the UI auto-refreshes every 60s showing active HITL requests, open incidents, circuit breaker states, and policy violations.

3. **External access** — an MCP server for Claude Code and other AI agents.

It is a separate FastAPI service (`evalgov-agent`, port 8003) that wraps the existing `governance-service` REST API and ClickHouse — no new data collection. All signals were already being gathered; this layer adds intelligence and interaction.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│                    EvalGov Agent Service (:8003)                  │
│                                                                    │
│  ┌──────────────────────────┐  ┌──────────────────┐               │
│  │  Chat Agent              │  │   MCP Server     │               │
│  │  (Claude Sonnet)         │  │  (/mcp/sse)      │               │
│  │  44 tools                │  │  44 tools        │               │
│  └──────────────┬───────────┘  └──────┬───────────┘               │
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
                                     └─────────────────────────────┘
```

---

## Service Startup

The chat agent and MCP server start automatically as part of a single process.

When the `evalgov-agent` container starts (via `docker compose up`), the following happens in order:

```python
# evalgov_agent/main.py

# 1. MCP server is mounted at import time — before the app even starts serving
try:
    from mcp_server import build_mcp_app
    app.mount("/mcp", build_mcp_app())   # /mcp/sse and /mcp/messages/ wired in
except Exception as exc:
    log.warning("mcp_server_unavailable", reason=str(exc))  # graceful degradation

# 2. FastAPI lifespan runs on startup
@asynccontextmanager
async def lifespan(app: FastAPI):
    db = AgentDB()
    db.ensure_tables()
    app.state.db = db
    yield                        # service is now live
```

After startup all capabilities are live on port 8003:

| Endpoint | Purpose |
|---|---|
| `POST /chat` | Chat agent — user prompts via Streamlit UI |
| `GET /system-state` | Live system state — HITL, incidents, CBs, policy violations |
| `GET /health` | Health check — used by Docker healthcheck and load balancers |
| `GET /mcp/sse`, `POST /mcp/messages/` | MCP SSE server — Claude Code and external agents |

**The MCP server has one safety net:** it is wrapped in `try/except` so if the `mcp` package is missing, the rest of the service starts cleanly without it. In the current Docker image `mcp` is in `requirements.txt`, so it always mounts successfully.

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

Used for eval content and trace analytics. These are read-only analytics queries with no business logic — direct ClickHouse is faster and avoids an unnecessary HTTP hop.

```
Tool function → get_db()._run(sql)  →  ClickHouse:9000  (TCP, native driver)
```

Tools that use this path: `get_recent_traces`, `get_agent_performance`, `get_cost_breakdown`, `get_error_rates`, `get_prompt_detail`, `search_prompts`, `get_eval_runs`, `get_benchmarks`, `get_eval_scores_detail`, `get_policy_decisions`, `get_routing_decisions`

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
Agent traces, eval scores by metric, cost breakdown by agent, error rates, prompt text + response by trace ID, recent prompt search, eval runs, benchmark definitions, eval scores with reasoning, policy decisions, model routing decisions

---

## Component 1: Chat Agent

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

---

## Chat UI Layout

The Streamlit page (`14_EvalGov_Agent.py`) uses a two-panel layout:

```
┌──────────────────────────────────────┬────────────────────────────┐
│  Chat (60%)                          │  Live System State (40%)   │
│                                      │                            │
│  [hint text on empty state]          │  🔔 HITL Queue (1)         │
│                                      │  ──────────────────────    │
│  user: what's wrong?                 │  hold · searcher           │
│  agent: [response with tool          │  2m ago · high             │
│          call disclosure]            │                            │
│                                      │  ⚡ Incidents (1)          │
│  user: approve hitl abc123           │  ──────────────────────    │
│  agent: Approved — agent will        │  P1 · searcher · open      │
│         proceed with search action   │                            │
│                                      │  🔌 Circuit Breakers (0)   │
│  [Ask anything about your AI         │  ──────────────────────    │
│   systems...]                        │  All closed                │
│                                      │  [↺ auto-refreshes 60s]    │
└──────────────────────────────────────┴────────────────────────────┘
```

**Chat panel features:**
- Clear Chat button resets session history
- Each assistant message shows an expandable "N tools called" disclosure with tool name, inputs, and result preview
- History is stateless (client-managed) — survives page reload by Streamlit session state

**Live system state panel features:**
- Auto-refreshes every 60s via `GET /system-state`
- Shows: pending HITL requests, open incidents, circuit breaker states, policy violations
- Expanders per category, scrollable within each section
- "All clear" message when no active issues in the last 24h

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | **Required** — used for the chat agent |
| `GOVERNANCE_SERVICE_URL` | `http://localhost:8002` | Internal governance-service URL |
| `CLICKHOUSE_HOST` | `localhost` | ClickHouse host |
| `CLICKHOUSE_PORT` | `9000` | ClickHouse native port |
| `CLICKHOUSE_DB` | `otel` | Database name |
| `AGENT_MODEL` | `claude-sonnet-4-6` | Claude model for the chat agent |
| `EVALGOV_AGENT_URL` | `http://localhost:8003` | Used by eval-ui and MCP clients |

---

## Integration with the Broader System

```
Governance tabs (pages 10, 11):
  → Human operators review and act on governance signals
  → Actions write to ClickHouse (HITL decisions, CB state changes)

EvalGov Agent (page 14):
  → Chat agent surfaces the same data conversationally via 44 tools
  → Live system state panel shows HITL, incidents, CBs, policy violations
  → Actions taken via chat write through governance-service REST API
  → MCP server exposes all tools to Claude Code and other AI agents
```

---

## Files

```
evalgov_agent/
├── main.py          FastAPI app, lifespan (ensure tables), REST endpoints
│                    Mounts MCP server at /mcp; exposes /chat, /system-state, /health
├── agent.py         Claude agent with tool use loop (up to 10 rounds per turn)
│                    SYSTEM_PROMPT with tool-use guidance and data-source rules
├── tools.py         44 tool implementations grouped by domain
│                    TOOL_MAP dict: name → function (dispatcher)
│                    TOOL_SCHEMAS list: LiteLLM/Anthropic JSON schemas for all 44 tools
│                    Two backends: _gov() for REST, get_db() for ClickHouse
│                    Bulk action tools: bulk_approve_hitl, bulk_resolve_incidents
├── db.py            AgentDB: ClickHouse TCP client (port 9000)
│                    Analytics: prompt_evals, eval_runs, benchmarks, eval_scores
│                    Analytics: gov_policy_decisions, gov_routing_decisions
│                    Token rate lookup from gov_threshold_config
├── mcp_server.py    MCP SSE server (Starlette ASGI, mounted at /mcp)
│                    list_tools() → same TOOL_SCHEMAS
│                    call_tool() → same execute_tool() → same tools.py
├── requirements.txt fastapi, uvicorn, clickhouse-driver, anthropic, mcp, httpx
└── Dockerfile       python:3.11-slim, port 8003

eval_ui/pages/
└── 14_EvalGov_Agent.py   Streamlit chat UI + live system state sidebar
```

---

## Related Documents

- `docs/llm-call-points.md` — **complete inventory of every LLM API call in the system**, models used, trigger conditions, token profiles, and cost estimates
- `docs/buildfwd-focus.md` — overall roadmap and next phases
- `docs/governance-enforcement-architecture.md` — three-layer enforcement architecture
- `docs/hitl-and-gate-ui-reference.md` — HITL and gate check UI reference
- `docs/agent-lightning-optimization.md` — AL-1/AL-2/AL-3 optimization phases
