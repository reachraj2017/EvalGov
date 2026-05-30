# opt-aieval SDK

Observability, evaluation, and governance instrumentation for multi-agent AI systems.

Connects any agent framework to the opt-aieval backend — traces flow to ClickHouse, Jaeger shows distributed traces, the eval-runner scores every trace with an LLM judge, and the governance service monitors for safety, cost, and policy violations — all automatically once spans are flowing.

For a platform overview (architecture, evaluation system, governance system, EvalGov Agent), see the [project README](../README.md).

---

## Prerequisites

The backend must be running before any spans can be received:

```bash
cd /path/to/opt-aieval
cp .env.example .env        # add ANTHROPIC_API_KEY
docker compose up -d
```

Seven services start: ClickHouse, Jaeger, OTel Collector, Eval Runner, Governance Service, EvalGov Agent, Eval UI.

---

## Install

```bash
# Clone the repo first, then install from the instrumentation/ directory
git clone https://github.com/your-org/opt-aieval.git
cd opt-aieval

# Pick the extras that match your agent framework
pip install -e ./instrumentation[langchain]
pip install -e ./instrumentation[autogen]
pip install -e ./instrumentation[crewai]
pip install -e ./instrumentation[claude]
pip install -e ./instrumentation[openai-agents]
pip install -e ./instrumentation[all]   # everything
```

---

## Step 1 — Initialize telemetry

Call this once at process startup, before any agent code runs.

```python
from instrumentation import init_telemetry

run_id = init_telemetry(
    service_name="my-research-agent",
    # otlp_endpoint defaults to localhost:4317
    # or set OTEL_EXPORTER_OTLP_ENDPOINT env var
)
```

This sets up the OTel tracer and auto-instruments Anthropic and OpenAI SDK calls via OpenLLMetry — every LLM call gets token counts, latency, and model name captured with no additional code.

---

## Step 2 — Instrument your agent

Choose the approach that fits your setup.

### Option A — Framework adapter (recommended if using a supported framework)

Adapters handle `agent.task`, `agent.tool_call`, and `agent.handoff` spans automatically.

**LangChain**
```python
from instrumentation import LangChainEvalAdapter

adapter = LangChainEvalAdapter(agent_id="lc-agent-1", agent_role="researcher")
agent.invoke(input, config={"callbacks": [adapter]})
```

**AutoGen**
```python
from instrumentation import AutoGenEvalAdapter

adapter = AutoGenEvalAdapter()
adapter.wrap_agent(my_agent, agent_id="ag-1", role="coder")
```

**CrewAI**
```python
from instrumentation import CrewAIEvalAdapter

adapter = CrewAIEvalAdapter()
task = Task(description="...", callback=adapter.task_callback, agent=my_agent)
```

**Anthropic / Claude SDK**
```python
from instrumentation import ClaudeEvalAdapter

adapter = ClaudeEvalAdapter(agent_id="claude-1", role="assistant")

with adapter.task("Summarize this document", run_id=run_id) as task:
    result = client.messages.create(...)
    task.set_output(result.content[0].text)
```

**OpenAI Agents SDK**
```python
from instrumentation import OpenAIAgentsEvalAdapter
from agents import Agent, Runner

adapter = OpenAIAgentsEvalAdapter(agent_id="oai-1", agent_role="assistant")
agent = Agent(name="assistant", instructions="...", hooks=adapter.hooks())
result = Runner.run_sync(agent, "What is the capital of France?")
```

---

### Option B — Manual span context managers (framework-agnostic)

Use these when you're not using a supported framework, or when you need precise control over span boundaries.

```python
from instrumentation import agent_task, agent_tool_call, agent_handoff

# Wrap the top-level agent entry point
with agent_task(agent_id="agent-1", role="researcher", task_input=user_query) as task:
    result = my_agent.run(user_query)
    task.set_output(result)
    task.set_status("success")   # defaults to "success" on clean exit

# Wrap individual tool calls
with agent_tool_call(agent_id="agent-1", tool_name="web_search", tool_input={"query": q}) as tool:
    results = web_search(q)
    tool.set_output({"results": results})

# Mark agent-to-agent delegation
with agent_handoff(from_agent_id="orchestrator", to_agent_id="searcher", reason="needs web data"):
    response = await searcher.run(sub_query)
```

**Critical attributes for full eval coverage:**

| Attribute | Required for |
|---|---|
| `task.input` + `task.output` | All LLM judges (faithfulness, relevance, hallucination, etc.) |
| `task.status = "success"\|"failure"` | Failure rate, error recovery metrics |
| `span.set_status(Status(StatusCode.ERROR))` on failure | `task_success` / `agent_failure` OTel-computed metrics |
| `tool.output` on tool spans | Hallucination judge grounding context |
| `run.id` on all spans | Grouping traces into a named run |
| `trace.source = "production"\|"benchmark"` | Source filter in Eval Measurements dashboard |
| `conversation.id` on all turns | Multi-turn evaluation (conversation completeness, knowledge retention) |

For the full attribute reference and span hierarchy diagram, see [`docs/instrumentation-guide.md`](../docs/instrumentation-guide.md) — the span schema, evaluator tables, and context re-attachment patterns for multi-agent traces are all documented there.

---

## Step 3 — Add governance enforcement (optional)

The governance service monitors every trace automatically once spans are flowing — no SDK changes needed for Phase 1 (trust scores, circuit breakers, safety, PII, SLO monitoring).

For Phase 2 **pre-execution blocking**, wrap tool calls with `GovernedToolkit`:

```python
from instrumentation import GovernedToolkit, GateBlockedError

toolkit = GovernedToolkit(agent_role="searcher")

# Wrap once — use governed_search exactly like the original
governed_search = toolkit.wrap("web_search", web_search)

try:
    results = governed_search(query="AI news")
except GateBlockedError as exc:
    # Circuit breaker is OPEN or gate hard-blocked this action
    return f"Unable to complete — {exc}"
```

Phase 2 enforcement is **off by default**. Enable it in the UI (Governance Enforcement page) or via API with no agent code changes required:

```bash
curl -X POST http://localhost:8002/enforcement/phase2/enable
```

For the full governance integration guide — gate decision flow, HITL patterns, runner-level CB check, ADK `before_tool_callback`, and multi-path agent gating — see [`docs/gov-instrument-guide.md`](../docs/gov-instrument-guide.md).

---

## Step 4 — Run benchmarks against your agent

The eval runner can execute structured test suites against your agent's HTTP endpoint.

**Expose a `/chat` endpoint** that accepts `run_id`, `source`, and `conversation_id`:

```python
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI()

class ChatRequest(BaseModel):
    message: str
    run_id: str | None = None
    source: str = "benchmark"
    conversation_id: str | None = None

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/chat")
def chat(body: ChatRequest):
    response, trace_id = run_agent(
        user_input=body.message,
        run_id=body.run_id,
        source=body.source,
        conversation_id=body.conversation_id,
    )
    return {"response": response, "trace_id": trace_id}
```

**Create a run and execute:**

```bash
# Create a named run
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent v1.0", "suite": "integration", "agent_version": "1.0.0"}'

# Execute the run against your agent
curl -X POST http://localhost:8000/runs/<run_id>/evaluate
```

Or use the **Runs** tab in the eval UI at `http://localhost:8501`.

---

## What you get automatically

Once spans are flowing, the eval pipeline runs three tiers on every trace with no configuration:

**OTel-computed** (instant, from span data): task latency, token count, cost estimate, tool call success rate, handoff rate, dead span rate.

**Deterministic** (every trace): task success rate, error recovery, context propagation fidelity, trace completeness, format compliance.

**LLM judges** (sampled — 15% of production traces, 100% on failures): faithfulness, relevance, hallucination, safety, instruction following, QA correctness, handoff fidelity.

**Multi-turn judges** (triggered when a `conversation.id` goes idle for 60s): conversation completeness, knowledge retention, role adherence, conversation relevancy.

---

## View results

| URL | What's there |
|---|---|
| `http://localhost:8501` | Eval Measurements dashboard — 56 metrics across 9 sections |
| `http://localhost:16686` | Jaeger — full distributed trace per run |
| `http://localhost:8003` | EvalGov Agent — ask natural language: *"show me cost by agent for today"* |
| `http://localhost:8000` | Eval Runner API — runs, benchmarks, scores |

---

## Public API

```python
from instrumentation import (
    # Setup
    init_telemetry,       # initialize OTel + auto-instrumentation
    get_tracer,           # shared tracer instance
    get_run_id,           # run_id set during init_telemetry()

    # Span context managers
    agent_task,           # one per agent invocation
    agent_tool_call,      # one per tool execution
    agent_handoff,        # one per agent-to-agent delegation
    agent_memory,         # one per memory read/write
    agent_decision,       # one per explicit decision point

    # Governance
    GovernedToolkit,      # pre-execution gate check wrapper
    GateBlockedError,     # raised when gate hard-blocks an action

    # Framework adapters
    LangChainEvalAdapter,
    AutoGenEvalAdapter,
    CrewAIEvalAdapter,
    ClaudeEvalAdapter,
    OpenAIAgentsEvalAdapter,
)
```

---

## Reference docs

| Document | What's in it |
|---|---|
| [`docs/instrumentation-guide.md`](../docs/instrumentation-guide.md) | Full span schema, attribute reference, evaluator tables, multi-agent context re-attachment patterns, framework-specific notes |
| [`docs/gov-instrument-guide.md`](../docs/gov-instrument-guide.md) | Phase 2 gate decision flow, HITL patterns, runner-level circuit breaker check, governance checklist |
| [`docs/eval-metrics-dashboard-guide.md`](../docs/eval-metrics-dashboard-guide.md) | How the Eval Metrics dashboard filters work, section-by-section breakdown |
| [`examples/opt-demo/`](../examples/opt-demo/) | Full working multi-agent demo (orchestrator, searcher, summarizer, translator) — reference implementation |
