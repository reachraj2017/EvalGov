# Instrumentation Guide: Adding Any Multi-Agent System to aieval

How to connect any multi-agent system — regardless of framework (Google ADK, LangChain, AutoGen, CrewAI, custom) — to the aieval observability and evaluation stack so all UI tabs, alerts, and dashboards work end-to-end.

Using this guide alone you should be able to instrument a new agent system and get the same eval metrics, traces, scoring, and alerts as the reference Demo 2 deployment.

---

## Architecture overview

```
Your agent code
     │
     │  OTel spans (gRPC :4317 or HTTP :4318)
     ▼
OTel Collector (aieval-otelcol)
     │
     ├──► ClickHouse  (permanent trace storage, SQL-queryable)
     ├──► Jaeger      (interactive trace visualisation)
     └──► Eval Runner (automated scoring + alert pipeline)
              │
              └──► ClickHouse  (eval_scores, prompt_evals, alert_thresholds)
```

All services run via `docker compose up -d`. Your only job is to emit correctly-structured OTel spans from your agent code.

---

## Step 1 — Install dependencies

Add to your `requirements.txt`:

```
opentelemetry-api>=1.20.0
opentelemetry-sdk>=1.20.0
opentelemetry-exporter-otlp-proto-grpc>=1.20.0
opentelemetry-instrumentation-openai>=0.28.0   # if using OpenAI or LiteLLM
fastapi>=0.100.0
uvicorn>=0.22.0
python-dotenv>=1.0.0
```

---

## Step 2 — Install the SDK and initialize telemetry

Install the opt-aieval instrumentation SDK, then call `init_telemetry()` once at process startup.

```bash
pip install -e ./instrumentation[all]   # from the opt-aieval repo root
```

```python
from instrumentation import init_telemetry

run_id = init_telemetry(
    service_name="my-agent-system",
    # otlp_endpoint defaults to localhost:4317
    # or set OTEL_EXPORTER_OTLP_ENDPOINT env var
)
```

This sets up the OTel tracer, auto-instruments Anthropic and OpenAI SDK calls, and returns a stable `run_id` for this session.

> **If you use Google ADK**, ADK creates its own OTel spans internally. The SDK uses a global TracerProvider; ADK's spans will flow through the same exporter. That is fine — they give the LLM call spans valid parents in the trace hierarchy. The eval runner only evaluates spans named `agent.task`, `agent.tool_call`, and `agent.handoff`.

> **For full SDK usage** including framework adapters (LangChain, AutoGen, CrewAI, Claude, OpenAI Agents SDK) and governance enforcement, see [`instrumentation/README.md`](../instrumentation/README.md).

---

## Step 3 — Set up `.env`

```bash
OPENAI_API_KEY=sk-...                              # your LLM key
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317  # OTel Collector (gRPC)
```

If your agent runs inside Docker on the same compose network:
```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector:4317
```

---

## Step 4 — Emit the right spans

The eval runner processes four span types. Everything else is stored in ClickHouse for Jaeger browsing but is not evaluated.

### Span name contract

| Span name         | When to emit                                       | Required attributes |
|-------------------|----------------------------------------------------|---------------------|
| `agent.task`      | One per agent invocation (start → end of task)     | `agent.id`, `agent.role`, `task.input`, `task.output`, `task.status`, `run.id` |
| `agent.tool_call` | One per external tool call                         | `agent.id`, `tool.name`, `tool.input`, `tool.output`, `task.status`, `run.id` |
| `agent.handoff`   | When one agent delegates to another                | `agent.id`, `handoff.target`, `task.status`, `run.id` |
| `agent.llm_call`  | One per LLM API call (if not auto-instrumented)    | `gen_ai.request.model`, `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens` |

> **LLM spans:** If you use OpenAI or LiteLLM auto-instrumentation (`OpenAIInstrumentor().instrument()`), LLM call spans are emitted automatically as `openai.chat` — you do not need `agent.llm_call`. The platform detects any span whose name is `agent.llm_call`, contains `llm`, or contains `chat` (case-insensitive).

### Attribute reference

| Attribute                      | Type   | Description                                               | Example                           |
|--------------------------------|--------|-----------------------------------------------------------|-----------------------------------|
| `agent.id`                     | string | Unique name for this agent instance                       | `"searcher"`, `"planner"`         |
| `agent.role`                   | string | Functional role (drives role-specific evaluators)         | `"orchestrator"`, `"researcher"`  |
| `task.id`                      | string | UUID for this invocation                                  | `str(uuid4())`                    |
| `task.input`                   | string | The prompt / instruction the agent received (≤ 2000 chars) | `user_input[:2000]`              |
| `task.output`                  | string | The agent's final response (≤ 2000 chars)                 | `response[:2000]`                 |
| `task.status`                  | string | `"success"` or `"failure"`                                |                                   |
| `tool.name`                    | string | Name of the tool called                                   | `"web_search"`, `"sql_query"`     |
| `tool.input`                   | string | Arguments passed to the tool (≤ 500 chars)                |                                   |
| `tool.output`                  | string | Result returned by the tool (≤ 500 chars)                 | needed for hallucination judge    |
| `handoff.target`               | string | Name of the agent being delegated to                      | `"summarizer"`                    |
| `run.id`                       | string | Shared run identifier — same value on ALL spans in a run  | UUID                              |
| `trace.source`                 | string | `"production"` or `"benchmark"` — set on root AND children | `"benchmark"`                   |
| `conversation.id`              | string | Shared ID across all turns of a multi-turn conversation   | UUID or session ID                |
| `gen_ai.request.model`         | string | LLM model name (on LLM call spans)                        | `"gpt-4o-mini"`                   |
| `gen_ai.usage.input_tokens`    | int    | Prompt tokens consumed (on LLM call spans)                | `1024`                            |
| `gen_ai.usage.output_tokens`   | int    | Completion tokens generated (on LLM call spans)           | `256`                             |

Aliases accepted: `gen_ai.usage.prompt_tokens` / `gen_ai.usage.completion_tokens` and `llm.model`.

### Span hierarchy

Structure spans so child spans are nested inside parent spans. The eval runner uses the parent-child relationship to understand the execution graph and attribute LLM tokens to the correct agent.

```
agent.task  [orchestrator]              ← root span: task.input = user query
  agent.task  [searcher]                ← child: task.input = sub-task instruction
    agent.tool_call  [web_search]       ← grandchild (tool.output needed for hallucination judge)
    openai.chat  [gpt-4o-mini]          ← auto-emitted by OpenAIInstrumentor
  agent.handoff  [→ summarizer]         ← child
  agent.task  [summarizer]              ← child
    openai.chat  [gpt-4o-mini]
  agent.task  [translator]              ← child
    openai.chat  [gpt-4o-mini]
```

**Token attribution rule:** Each LLM span's tokens are attributed to its nearest `agent.task` ancestor. The orchestrator row in Prompt Lab X shows the trace total (all sub-agents summed). Sub-agent rows each show only their own slice.

---

## Step 5 — Implement `runner.py`

The runner is where all span creation happens. The patterns below are the exact patterns used in Demo 2. Follow them precisely — deviating (especially from the context re-attachment pattern) will cause spans to appear in separate traces.

### Root span

```python
import uuid
from opentelemetry import trace, context as otel_context
from opentelemetry.trace import Status, StatusCode
from instrumentation import init_telemetry, get_tracer, get_run_id

init_telemetry()   # call once at module load — idempotent


def run_agent(
    user_input: str,
    user_id: str = "default",
    run_id: str | None = None,
    source: str = "production",
    conversation_id: str | None = None,
) -> tuple[str, str]:
    """Run the agent and return (response_text, trace_id)."""
    tracer = get_tracer()
    run_id = run_id or get_run_id()

    with tracer.start_as_current_span("agent.task") as root_span:
        root_span.set_attribute("agent.id",     "orchestrator")
        root_span.set_attribute("agent.role",   "orchestrator")
        root_span.set_attribute("task.id",      str(uuid.uuid4()))
        root_span.set_attribute("task.input",   user_input[:2000])
        root_span.set_attribute("run.id",       run_id)
        root_span.set_attribute("trace.source", source)   # ← set on root
        if conversation_id:
            root_span.set_attribute("conversation.id", conversation_id)

        # Capture trace_id while root span is active
        ctx      = root_span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx and ctx.trace_id else ""

        # Capture root context for re-attachment in child calls
        root_ctx = otel_context.get_current()

        try:
            response = _run_logic(
                user_input, root_span, root_ctx, tracer, run_id, source, conversation_id
            )
            root_span.set_attribute("task.output",  response[:2000])
            root_span.set_attribute("task.status",  "success")
        except Exception as e:
            root_span.set_attribute("task.status",   "failure")
            root_span.set_status(Status(StatusCode.ERROR, str(e)))   # ← needed for task_success / agent_failure metrics
            root_span.record_exception(e)
            raise

    return response, trace_id
```

> **`set_status(Status(StatusCode.ERROR))`** — this is required alongside `record_exception()`. The OTel-computed metrics `task_success` and `agent_failure` read the OTel span StatusCode, not the `task.status` string attribute. Without `set_status`, the span registers as successful even when an exception is raised and the `task_success` metric will be incorrect.

### Child agent spans

The critical pattern for sequential child agent calls is **re-attaching the root context** before each call. Without this, context mutations by ADK or other frameworks will break the trace hierarchy and child spans will appear in a separate trace.

```python
async def _call_agent(
    agent_name: str,
    message: str,
    root_span,
    root_ctx,
    tracer,
    run_id: str,
    source: str = "production",
    conversation_id: str | None = None,
) -> str:
    """
    Call a sub-agent and wrap it in an agent.task child span.

    Re-attaches root OTel context before each call so all spans land in the
    same trace regardless of framework-internal context mutations.
    """
    # Step 1: re-attach root context so this child is in the same trace
    token      = otel_context.attach(root_ctx)
    parent_ctx = trace.set_span_in_context(root_span)

    child_span  = tracer.start_span("agent.task", context=parent_ctx)
    child_token = None

    child_span.set_attribute("agent.id",     agent_name)
    child_span.set_attribute("agent.role",   agent_name)
    child_span.set_attribute("task.id",      str(uuid.uuid4()))
    child_span.set_attribute("task.input",   message[:2000])
    child_span.set_attribute("run.id",       run_id)
    child_span.set_attribute("trace.source", source)   # ← required on ALL spans, not just root
    if conversation_id:
        child_span.set_attribute("conversation.id", conversation_id)

    # Step 2: make child_span the active context so any LLM auto-instrumentation
    # calls (openai.chat spans) are nested inside this agent span, not the root.
    child_token = otel_context.attach(trace.set_span_in_context(child_span))
    child_ctx   = trace.set_span_in_context(child_span)

    output_text = ""
    try:
        output_text = await your_agent_call(message)   # replace with your framework call
    except Exception as e:
        child_span.set_attribute("task.status",  "failure")
        child_span.set_status(Status(StatusCode.ERROR, str(e)))
        child_span.record_exception(e)
        raise
    finally:
        if child_token is not None:
            otel_context.detach(child_token)
        otel_context.detach(token)

    child_span.set_attribute("task.output", output_text[:2000])
    child_span.set_attribute("task.status", "success")
    child_span.end()

    return output_text
```

> **Why re-attach root context?** Frameworks like Google ADK modify OTel context internally during `run_async`. After each agent call returns, the context on the thread is no longer the root context. The next child call would create a span parented to the wrong context — outside the root trace. Re-attaching `root_ctx` at the top of each call guarantees correct nesting.

> **Why attach child_span as active context?** OpenAI auto-instrumentation uses `trace.get_current_span()` to find its parent. If the child `agent.task` span is not the current active span when the LLM call is made, the `openai.chat` span will parent to the root span instead of the agent span. This breaks the trace hierarchy and token attribution.

### Tool call spans

Include `tool.output` — the hallucination judge uses it as grounding context.

```python
def _emit_tool_span(
    tool_name: str,
    tool_input: str,
    tool_output: str,
    agent_name: str,
    tracer,
    run_id: str,
    parent_ctx,
) -> None:
    span = tracer.start_span("agent.tool_call", context=parent_ctx)
    span.set_attribute("agent.id",    agent_name)
    span.set_attribute("tool.name",   tool_name)
    span.set_attribute("tool.input",  tool_input[:500])
    span.set_attribute("tool.output", tool_output[:500])   # ← required for hallucination judge
    span.set_attribute("run.id",      run_id)
    span.set_attribute("task.status", "success")
    span.end()
```

### Manual LLM call span (only if NOT using OpenAI auto-instrumentation)

```python
def _emit_llm_span(
    model: str,
    in_tokens: int,
    out_tokens: int,
    tracer,
    parent_ctx,
) -> None:
    span = tracer.start_span("agent.llm_call", context=parent_ctx)
    span.set_attribute("gen_ai.request.model",       model)
    span.set_attribute("gen_ai.usage.input_tokens",  in_tokens)
    span.set_attribute("gen_ai.usage.output_tokens", out_tokens)
    span.end()
```

> **Per-agent model attribution:** For multi-agent traces where different agents use different models (e.g. Qwen for search, BART for summarize, NLLB for translate), each agent's `agent.llm_call` span **must be nested inside that agent's `agent.task` span** — not the root span. The eval pipeline walks `parent_span_id` links to attribute each LLM span to its owning agent task span. If all `agent.llm_call` spans are children of the root, every agent row in Prompt Analysis will show the same model (whichever is found first). Use `context=trace.set_span_in_context(child_span)` when starting the `agent.llm_call` span inside a sub-agent handler.

### Axis 3 — SLM specialization span attributes (optional)

If you replace LLM calls with purpose-built SLMs (e.g. BART for summarization, NLLB for translation, Qwen for synthesis) and want the SLM metadata visible in traces, add these to the owning `agent.task` span alongside the standard `agent.llm_call` child span:

| Attribute | Type | Description | Example |
|---|---|---|---|
| `ios.axis3.model_used` | string | HuggingFace model ID used for this task | `"facebook/bart-large-cnn"` |
| `ios.axis3.model_params_b` | float | Model size in billions of parameters | `0.4` |
| `ios.axis3.confidence_score` | float | Per-task confidence gate score (0–1) | `0.82` |
| `ios.axis3.escalated` | bool | Whether the SLM output was below threshold and escalated to an LLM | `false` |

These are custom attributes — the eval pipeline does not process them for scoring, but they appear in Jaeger traces for debugging confidence gating and escalation behaviour. The standard `agent.llm_call` child span with `gen_ai.*` attributes is still required for token counts and Prompt Analysis.

---

## Step 6 — Create `api.py`

The eval runner calls your agent over HTTP when executing benchmarks. Expose a `/chat` endpoint. Copy this verbatim and update the import:

```python
"""
HTTP API.

Start:
    uvicorn api:app --host 0.0.0.0 --port 8080 --reload

Endpoints:
    GET  /health  → liveness probe
    POST /chat    → run agent, return response + trace_id
"""

import os
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from runner import run_agent   # ← your instrumented runner

app = FastAPI()


class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"
    run_id: str | None = None           # eval runner passes this to group traces
    source: str = "benchmark"           # "benchmark" | "production"
    conversation_id: str | None = None  # groups turns for multi-turn eval


class ChatResponse(BaseModel):
    response: str
    trace_id: str
    run_id: str | None = None
    source: str = "benchmark"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
def chat(body: ChatRequest):
    if not body.message.strip():
        raise HTTPException(status_code=400, detail="message cannot be empty")
    try:
        response, trace_id = run_agent(
            user_input=body.message,
            user_id=body.user_id,
            run_id=body.run_id,
            source=body.source,
            conversation_id=body.conversation_id,
        )
        return ChatResponse(
            response=response,
            trace_id=trace_id,
            run_id=body.run_id,
            source=body.source,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
```

Start it alongside your agent:
```bash
uvicorn api:app --host 0.0.0.0 --port 8080 --reload
```

---

## Step 7 — Create a Run and run benchmarks

### 7a — Register a named run

```bash
curl -X POST http://localhost:8000/runs \
  -H "Content-Type: application/json" \
  -d '{"name": "my-agent v1.0", "suite": "integration", "agent_version": "1.0.0"}'
# Returns: {"run_id": "<uuid>"}
```

Or via the UI: **Runs** tab → **Create New Run** → copy the `run_id`.

### 7b — Add benchmarks

**Benchmarks** tab → **Add Benchmark**. Fields:
- **Suite** — grouping label
- **Name** — test case name
- **Task Input** — the prompt to send
- **Expected Output** (optional) — enables `qa_correctness` judge
- **Rubric** (optional JSON) — enables `custom_rubric` judge: `{"criteria": ["criterion 1", "criterion 2"]}`

### 7c — Execute

**Runs** tab → find your run → click ▶ (Execute). The eval runner POSTs each benchmark task to your `/chat` endpoint with `run_id` and `source="benchmark"` in the request body.

Or via API:
```bash
curl -X POST http://localhost:8000/runs/<run_id>/evaluate
```

> **Important:** your `run_agent()` function must accept `run_id` and `source` and stamp every span with them — including all child agent spans. If `trace.source` is not propagated to children, the source filter in Prompt Lab X will be broken for those sub-agent rows.

---

## Step 8 — Multi-turn conversations

To enable multi-turn evaluation, pass a stable `conversation_id` on every span across all turns of the same session:

```python
conversation_id = str(uuid.uuid4())   # generate once per user session

# Turn 1
response1, _ = run_agent(user_input="Hello", conversation_id=conversation_id)

# Turn 2 (same session)
response2, _ = run_agent(user_input="What did you just say?", conversation_id=conversation_id)
```

Pass `conversation_id` through `run_agent()` → root span → every child span (see Step 5 patterns above).

The eval runner automatically triggers multi-turn evaluation (conversation completeness, knowledge retention, role adherence) when a conversation goes idle for 60 seconds. Results appear in the Scores tab filtered by `eval_type = multi_turn`.

---

## Step 9 — Framework-specific notes

### Google ADK

ADK creates its own internal OTel spans when `trace.set_tracer_provider()` sets the global provider. These flow through the same exporter. Leave them — they provide valid parent spans for `openai.chat` LLM spans in the trace hierarchy. The eval runner only processes spans named `agent.task`, `agent.tool_call`, `agent.handoff`, and LLM spans (`agent.llm_call` / `openai.chat`).

The most important ADK-specific pattern is the **context re-attachment** in `_call_agent()` (Step 5). ADK mutates the OTel context during `runner.run_async()`. Without re-attaching `root_ctx` before each sequential agent call, subsequent child spans will be orphaned into separate traces.

### OpenAI / LiteLLM auto-instrumentation

```python
from opentelemetry.instrumentation.openai import OpenAIInstrumentor
OpenAIInstrumentor().instrument()
```

Call this once after `trace.set_tracer_provider(provider)` (it's already in `init_telemetry()` above). LLM call spans (`openai.chat`) are automatically emitted with `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, and `gen_ai.request.model` — no manual `agent.llm_call` spans needed.

Make sure the relevant `agent.task` child span is the **active context** when the LLM call is made (see the `child_token = otel_context.attach(...)` pattern in Step 5). If it isn't, token attribution and trace hierarchy will be wrong.

### LangChain

The OTel Collector normalises LangChain attributes automatically (already configured in `infra/otelcol/config.yaml`). Enable LangChain's OTel exporter and point it at the collector. You may still want to add your own `agent.task` wrapper spans for the root and named sub-agents so the eval runner can score them.

### AutoGen / CrewAI

Framework attribute normalisation is also pre-configured in `infra/otelcol/config.yaml`. If your framework uses attribute names not already mapped, add a `transform/normalize` rule and restart the collector: `docker compose restart otel-collector`.

---

## Step 10 — Verify traces are arriving

1. Open Jaeger at `http://localhost:16686`
2. Select your `service.name` from the dropdown
3. Confirm `agent.task` spans appear with the correct parent-child hierarchy
4. Confirm `openai.chat` LLM spans are nested inside the correct `agent.task` spans (not floating at root level)

If no traces appear:
- Check collector health: `curl http://localhost:13133/` → `{"status":"Server available"}`
- Confirm endpoint — gRPC `4317`, HTTP `4318`
- Check collector logs: `docker logs aieval-otelcol`

---

## What gets evaluated automatically

The eval pipeline runs three tiers on every completed trace. No configuration required — scores are written to ClickHouse within seconds.

### OTel-computed metrics (every trace, `eval_type = otel_computed`)

These are computed directly from span data.

| Metric                        | Requires                                          | What it measures                          |
|-------------------------------|---------------------------------------------------|-------------------------------------------|
| `task_success`                | root `agent.task` span OTel StatusCode            | 1.0 = no error, 0.0 = StatusCode.ERROR   |
| `agent_failure`               | root `agent.task` span OTel StatusCode            | 1.0 = failure, 0.0 = success             |
| `task_latency_ms`             | root `agent.task` span duration                   | End-to-end wall-clock time (ms)           |
| `timeout`                     | root `agent.task` span duration                   | 1.0 if latency > 30 s, else 0.0          |
| `token_count`                 | `gen_ai.usage.*` on LLM spans                     | Total input + output tokens across trace  |
| `cost_usd`                    | `gen_ai.usage.*` on LLM spans                     | Estimated cost (gpt-4o-mini pricing)      |
| `context_window_utilization`  | `gen_ai.usage.input_tokens` on LLM spans          | Fraction of 128k context used (max call)  |
| `tool_call_success_rate`      | `agent.tool_call` spans + `task.status`           | Fraction of tool calls that succeeded     |
| `tool_calls_per_task`         | `agent.tool_call` spans                           | Total tool calls in the trace             |
| `handoff_success_rate`        | `agent.handoff` spans + `task.status`             | Fraction of handoffs that succeeded       |
| `agent_hop_latency_ms`        | `agent.handoff` span durations                    | Average time per handoff (ms)             |
| `dead_span_rate`              | all spans + parent IDs                            | Fraction of unlinked / orphaned spans     |

> **`task_success` / `agent_failure`** read the OTel `StatusCode` set by `span.set_status(Status(StatusCode.ERROR))`, **not** the `task.status` string attribute. You must call `set_status` on failure — `record_exception` alone is not enough.

### Deterministic evaluators (every trace)

| Metric                          | What it measures                                               |
|---------------------------------|----------------------------------------------------------------|
| `task_success_rate`             | Task span completed without `STATUS_CODE_ERROR`               |
| `agent_failure_rate`            | Fraction of agent spans that errored                           |
| `tool_accuracy`                 | Whether the right tools were called                            |
| `tool_error_rate`               | Fraction of tool calls that returned errors                    |
| `tool_retry_rate`               | How often tools are retried                                    |
| `handoff_success_rate`          | Fraction of agent handoffs that succeeded                      |
| `timeout_rate`                  | Fraction of spans that exceeded a time threshold               |
| `error_recovery_rate`           | Whether the system recovered after an agent failure            |
| `context_propagation_fidelity`  | Whether `run.id` / trace context flows through all spans       |
| `trace_completeness_rate`       | Whether all expected span types are present                    |
| `dead_span_rate`                | Fraction of orphaned (unlinked) spans                          |
| `step_efficiency`               | LLM + tool calls vs expected minimum for task complexity       |
| `format_compliance`             | Structured output matches expected schema                      |

### LLM judges (sampled — 15% of production traces, 100% on failures)

| Metric                     | What it measures                                                          |
|----------------------------|---------------------------------------------------------------------------|
| `hallucination_score`      | Absence of invented facts not grounded in context (1.0 = clean)          |
| `safety_score`             | Output free from toxicity, PII, harmful content (1.0 = safe)             |
| `faithfulness`             | Claims are grounded in provided sources                                   |
| `relevance`                | Output addresses the actual task input                                    |
| `instruction_following`    | All explicit constraints in the prompt were respected                     |
| `qa_correctness`           | Answer matches `expected_output` from benchmark (skipped if not set)     |
| `custom_rubric`            | Output satisfies criteria in `rubric` JSON from benchmark (skipped if not set) |
| `handoff_fidelity`         | Orchestrator delegated to the correct agent with the correct context      |

#### Multi-turn judges (triggered when `conversation.id` spans go idle for 60 s)

| Metric                      | What it measures                                              |
|-----------------------------|---------------------------------------------------------------|
| `conversation_completeness` | The user's original goal was fully resolved across all turns  |
| `conversation_relevancy`    | Each agent response stayed on-topic across turns              |
| `knowledge_retention`       | Agent correctly used information from earlier turns           |
| `role_adherence`            | Agent maintained its assigned role across turns               |

---

## Alerts and Thresholds

Go to the **Alerts and Thresholds** tab to configure metric-level alerting. Click any eligible metric tile to set a threshold operator (`>`, `<`, `>=`, `<=`) and value. The violations feed shows all breaching traces within the configurable time window with prompt/response context.

Thresholds can be set on any metric in `eval_scores` — including `task_latency_ms` (`> 5000`), `cost_usd` (`> 0.05`), `token_count` (`> 50000`), `hallucination_score` (`< 0.7`), `safety_score` (`< 0.9`), etc.

---

## Step 11 — Verify end-to-end

| Tab / Feature              | What to verify                                                                  |
|----------------------------|---------------------------------------------------------------------------------|
| **Traces**                 | Span tree shows root → child agents → tool/LLM calls in correct nesting        |
| **Scores**                 | Scores appear within seconds; OTel-computed + deterministic + LLM judge tiers  |
| **Prompt Lab X**           | Each agent has its own row; orchestrator row = trace total tokens               |
| **Eval Metrics**           | Latency, cost, token count trends update correctly                              |
| **Alerts and Thresholds**  | Violation feed shows breaching traces with prompt/response context              |
| **Regression**             | Select two runs → click Compare for per-metric deltas                          |

---

## Recommended file structure

```
my-agent/
├── runner.py                  ← all OTel span creation (root + child spans), Step 5
├── api.py                     ← FastAPI /chat endpoint, Step 6
├── agents/                    ← agent definitions (no instrumentation needed here)
├── tools/                     ← tool implementations (no instrumentation needed here)
├── requirements.txt           ← your deps + opt-aieval SDK
└── .env                       ← LLM API key + OTEL_EXPORTER_OTLP_ENDPOINT
```

Start order:
```bash
uvicorn api:app --host 0.0.0.0 --port 8080   # agent API (for benchmark execution)
```

Both `api.py` and any chat UI call `init_telemetry()` from the SDK — telemetry initialises once per process.

---

## Quick reference

| What                        | Where                                                |
|-----------------------------|------------------------------------------------------|
| OTel ingest (gRPC)          | `localhost:4317`                                     |
| OTel ingest (HTTP)          | `localhost:4318`                                     |
| Trace visualisation         | `http://localhost:16686` (Jaeger)                    |
| Eval scores UI              | `http://localhost:8501`                              |
| EvalGov Agent               | `http://localhost:8003`                              |
| Governance API              | `http://localhost:8002`                              |
| Eval runner API             | `http://localhost:8000`                              |
| ClickHouse HTTP             | `http://localhost:8123`                              |
| Collector health            | `http://localhost:13133`                             |
| Collector config            | `infra/otelcol/config.yaml`                          |
| Evaluator config            | `eval_runner/config/evaluator_config.yaml`           |
| Custom evaluators           | `eval_runner/evaluators/`                            |
| Persistent data             | `./data/clickhouse`, `./data/jaeger`                 |

---

## Attribute checklist

```
# Required on every agent.task span
agent.id       = <agent name>               # e.g. "orchestrator", "searcher"
agent.role     = <agent name or role>       # used by role-specific evaluators
task.input     = <prompt text>[:2000]       # stored in prompt_evals, scored by all judges
task.output    = <response text>[:2000]     # scored by all judges
run.id         = <uuid>                     # groups traces into a run
task.status    = "success" | "failure"      # drives failure-rate metrics

# Required for correct OTel-computed metrics
# (set on failure in addition to task.status = "failure")
# from opentelemetry.trace import Status, StatusCode
span.set_status(Status(StatusCode.ERROR))   # enables task_success / agent_failure metrics

# Strongly recommended
task.id        = <uuid>                     # unique per invocation
trace.source   = "production" | "benchmark" # MUST be set on root AND all child spans

# For multi-turn conversations
conversation.id = <session uuid>            # same value across all turns of a session

# On agent.tool_call spans
tool.output    = <result text>[:500]        # grounding context for hallucination judge

# On LLM call spans (auto-set by OpenAIInstrumentor; or set manually on agent.llm_call)
gen_ai.request.model       = <model name>   # e.g. "gpt-4o-mini"
gen_ai.usage.input_tokens  = <int>          # enables token_count, cost_usd metrics
gen_ai.usage.output_tokens = <int>          # enables token_count, cost_usd metrics
```

**Missing `task.input` / `task.output`** → LLM judges score nothing.  
**Missing `gen_ai.usage.*`** → token_count, cost_usd, context_window_utilization show 0.  
**Missing `conversation.id`** → multi-turn evaluators do not run.  
**Missing `set_status(StatusCode.ERROR)` on failure** → task_success / agent_failure metrics show 1.0 / 0.0 even for failed spans.  
**Missing `trace.source` on child spans** → source filter in Prompt Lab X shows wrong source for sub-agents.  
**Missing `tool.output` on tool spans** → hallucination judge has no grounding context.

---

## Governance layer

The governance service runs as a sidecar watcher (`governance-service:8002`). It polls for new traces every 30 seconds and automatically evaluates every `agent.task` span against all 13 governance categories — safety, PII, reliability, supply chain, policy, anomaly detection, incidents, and more.

**No instrumentation changes are required.** If your agent emits correctly-structured spans per the steps above, all core governance monitoring activates automatically.

---

### What runs automatically (zero changes)

| Governance check | What it reads | What it produces |
|---|---|---|
| Safety guard | `task.input`, `task.output` | Injection / jailbreak / toxic / bias detection events; P1–P2 incidents |
| PII detection | `task.input`, `task.output` | PII event log, leak rate metric |
| Reliability / SLO | Span `StatusCode`, `Duration` | Error rate, p95/p99 latency, availability, error budget consumed; SLA breach incidents |
| Anomaly detection | Aggregated span metrics | Flags statistical outliers on error rate, latency, token usage; critical anomaly incidents |
| Supply chain check | `gen_ai.request.model` (auto-set by OpenAIInstrumentor) | Flags models not registered in the supply chain registry |
| Policy enforcement | `agent.role`, `task.input`, `task.output` | Block / warn / allow decisions per configured policy rules |
| Identity checks | `agent.role` | Flags unregistered agent roles, credential patterns in output |
| Incident management | Derived from all of the above | Auto-creates P1/P2 incidents with dedup, MTTD/MTTC/MTTR tracking |

> The governance watcher only evaluates spans named `agent.task`. It uses the same span filter as the eval runner. Ensure `agent.role` is set on every `agent.task` span — governance results are grouped and reported by role.

---

### Optional span attributes that unlock specific governance tabs

These three attributes require a one-time addition to your `agent.task` spans. None of them affect existing evaluation — they only enable features that are otherwise inactive.

#### `prompt.template_id` and `prompt.snapshot_hash` — Explainability tab

Enables prompt drift detection: tracks when the prompt content for a given template changes between deployments and surfaces an audit trail of which prompt hash was active when.

```python
import hashlib

with tracer.start_as_current_span("agent.task") as span:
    # ... existing attributes ...
    prompt_text = build_prompt(user_input)   # your prompt construction
    span.set_attribute("prompt.template_id",   "my_agent_v1")   # stable template name
    span.set_attribute("prompt.snapshot_hash",
                       hashlib.sha256(prompt_text.encode()).hexdigest()[:16])
```

- `prompt.template_id` — a stable string that identifies the prompt template, not the content (e.g. `"searcher_v1"`, `"planner_v2"`). Should not change between runs unless you intentionally version the template.
- `prompt.snapshot_hash` — a hash of the actual prompt content used in this invocation. The first hash seen for a `template_id` is stored as the baseline; any subsequent change is flagged as a drift event.

Without these attributes the Explainability tab shows `—` for all metrics.

#### `deployment.mode` — Lifecycle tab canary/shadow detection

Enables the Lifecycle tab to separate canary or shadow traffic from production traffic.

```python
span.set_attribute("deployment.mode", "canary")   # "canary" | "shadow" | "stable"
```

Set this on the root `agent.task` span and propagate it to all child spans the same way you propagate `trace.source`. Without it, all traffic is treated as production and canary/shadow rows in the Lifecycle tab remain empty.

---

### Governance configuration (UI only — no code changes)

These are one-time setup steps in the **AI Governance** page. They do not require any changes to agent code.

#### Supply chain registry

Register every model your agent uses so supply chain checks pass silently. Go to **AI Governance → Identity & Access → Supply Chain Registry → Register new artifact**.

- **Artifact type:** `model`
- **Artifact name:** the base model name as it appears in `gen_ai.request.model` — use `gpt-4o-mini`, **not** `openai/gpt-4o-mini`. The provider prefix is stripped by OTel instrumentation before the span attribute is written; the registry lookup matches on the stripped name.
- **Verified:** ON = passes silently. OFF = flags every trace as unverified.

Unregistered models produce `not_in_registry` identity events on every trace.

#### SLO configuration

Set performance targets per agent role. Go to **AI Governance → Reliability → SLO Configuration**.

| Field | Default | When to change |
|---|---|---|
| Target availability | 0.999 | Lower for non-critical / experimental agents |
| Error budget % | 0.001 | Fraction of errors allowed over the window |
| SLO window days | 30 | Lookback for error budget computation |
| p95 target (ms) | 2000 | Adjust to your agent's expected latency |
| p99 target (ms) | 5000 | |

Use `__default__` as the agent role to apply targets to all roles that don't have their own row. An SLA breach (availability below target) automatically creates a P2 incident.

#### Safety rules

Custom regex patterns for injection, jailbreak, toxic, and bias detection. Go to **AI Governance → Safety & Guardrails → Add safety rule**.

- Pattern must be a plain Python regex — **no JS-style `/pattern/flags` delimiters**
- Rule type: `injection`, `jailbreak`, `toxic`, or `bias`
- Rules activate immediately on the next trace — no restart needed
- Toxic/bias patterns are checked against the full combined input + output text of every `agent.task` span

---

### Optional: gate check API (pre-execution HITL)

If you want human-in-the-loop enforcement before a sensitive agent action executes, call the gate check endpoint before running the action. This is entirely optional — omitting it has no effect on any other governance monitoring.

```python
import httpx

def gate_check(trace_id: str, agent_role: str, action_type: str, payload: dict) -> bool:
    """Returns True if the action is approved to proceed."""
    try:
        r = httpx.post(
            "http://governance-service:8002/gate/check",
            json={
                "trace_id":    trace_id,
                "agent_role":  agent_role,
                "action_type": action_type,   # e.g. "tool_call", "data_access"
                "payload":     payload,
            },
            timeout=5.0,
        )
        return r.json().get("decision") == "allow"
    except Exception:
        return True   # fail open — let the action proceed if governance is unavailable
```

If agent API key auth is enabled (disabled by default), add the header:
```python
headers={"X-Agent-Key": os.getenv("GOVERNANCE_API_KEY", "")}
```

Gate check results appear in the **AI Governance → Policy** tab under the remediation log.

---

### Governance checklist

```
# Automatic — no changes needed if spans are already correct
agent.role     on every agent.task span   # governance results grouped by role
task.input     on every agent.task span   # safety + PII scanning
task.output    on every agent.task span   # safety + PII scanning
gen_ai.request.model  on LLM spans        # supply chain check (auto-set by OpenAIInstrumentor)
StatusCode     set on failure             # reliability / SLA breach detection

# Optional — add to unlock specific governance features
prompt.template_id    on agent.task spans  # Explainability tab — prompt drift baseline
prompt.snapshot_hash  on agent.task spans  # Explainability tab — drift detection
deployment.mode       on agent.task spans  # Lifecycle tab — canary/shadow separation

# One-time UI configuration (no code changes)
# - Register models in Supply Chain Registry (Identity & Access tab)
# - Set SLO targets per agent role (Reliability tab)
# - Add custom safety rules if needed (Safety & Guardrails tab)
```
