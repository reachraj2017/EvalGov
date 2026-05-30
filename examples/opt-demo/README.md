# Multi-Agent Demo 2 (Primary Demo)

A 4-agent system built with **Google ADK** and **OpenAI** with full multi-turn conversation tracking, smart rule-based query routing, and complete OTel instrumentation for the AI Eval & Observability Platform.

> **Platform must be running** before launching the demo.
> From the repo root: `make up`

---

## What Makes This Demo Different from Demo 1

| Feature | Demo 1 | Demo 2 |
|---|---|---|
| Query routing | LLM decides (unreliable on multi-step) | Rule-based classifier (reliable) |
| Multi-step pipeline | Orchestrator-driven | Explicit code: searcher → summarizer → translator |
| `conversation.id` propagation | Orchestrator only | All agent spans (orchestrator + sub-agents) |
| Target language | Hindi only | Any language specified in query |
| Summary word count | Fixed 20 words | Extracted from query (e.g. "in 30 words") |
| Backend | Streamlit direct | FastAPI + Streamlit |

---

## Agents

| Agent | Role | Default model | SLM alternative (Axis 3) |
|---|---|---|---|
| **Orchestrator** | Classifies intent, routes or answers passthrough queries | gpt-4o-mini | — |
| **Searcher** | Web search via DuckDuckGo, synthesises answer with citations | gpt-4o-mini | Qwen3-0.6B synthesis |
| **Summarizer** | Summarises text in specified word count | gpt-4o-mini | BART-large-cnn |
| **Translator** | Translates to any target language | gpt-4o-mini | NLLB-200 |

> With `AXIS3_SLM_ENABLED=true` (default), the searcher, summarizer, and translator each try a local SLM first. If the confidence score is below threshold, they automatically fall back to gpt-4o-mini. See [Axis 3 — SLM Specialization](#axis-3--slm-specialization) below.

---

## Query Routing

The demo classifies queries before calling any LLM:

| Pattern | Route |
|---|---|
| `search … and summarize` / `search … and translate` | `_run_pipeline()`: searcher → summarizer → translator |
| `search only` | Searcher directly (child span) |
| `summarize only` | Summarizer using session history (child span) |
| `translate only` | Translator using session history (child span) |
| `summarize + translate` (no search) | Summarizer → translator from session history |
| Everything else | Orchestrator passthrough (no child span) |

---

## Axis 3 — SLM Specialization

Three tasks (search synthesis, summarization, translation) can run on local HuggingFace SLMs instead of gpt-4o-mini. Each SLM call is confidence-gated — low-confidence outputs automatically escalate to gpt-4o-mini.

| Task | SLM | Confidence threshold |
|---|---|---|
| Search synthesis | Qwen3-0.6B | 0.80 |
| Summarization | BART-large-cnn | 0.75 |
| Translation | NLLB-200-distilled-600M | 0.85 |

### Flags in `.env`

```bash
# Enable/disable SLMs entirely
AXIS3_SLM_ENABLED=true        # true = SLMs with gpt-4o-mini fallback (default)
AXIS3_SLM_ENABLED=false       # false = gpt-4o-mini only

# Choose inference backend (only relevant when AXIS3_SLM_ENABLED=true)
AXIS3_SLM_BACKEND=local       # in-process CPU, lazy-loads on first request (default)
AXIS3_SLM_BACKEND=server      # dedicated FastAPI server, MPS GPU on Apple Silicon

AXIS3_SLM_SERVER_URL=http://localhost:8001   # server endpoint (server mode only)
```

Restart Streamlit after changing any flag.

### Running with the SLM server (MPS GPU, recommended for Apple Silicon)

The SLM server loads all three models once at startup onto the Apple Silicon GPU and serves them persistently. This eliminates cold-start latency and gives 3–6× faster inference vs CPU.

```bash
# Terminal 1 — start the SLM server (keep running)
cd examples/opt-demo
./start_slm_server.sh

# Wait for: [slm_server] All models ready on mps

# Terminal 2 — set backend and start Streamlit
# In .env: AXIS3_SLM_BACKEND=server
streamlit run chat_ui.py
```

Check server health:
```bash
curl http://localhost:8001/health
# {"status": "ready", "device": "mps"}
```

For full technical details — model architectures, concurrency model, memory implications, and backend comparison — see [`docs/slm-hf-models.md`](../../docs/slm-hf-models.md).

---

## Multi-Turn Conversation Tracking

Every chat session is assigned a `conversation_id` UUID when the session starts or when "New Conversation" is clicked. This is threaded through the entire call chain:

```
run_agent(conversation_id=...) 
  → root agent.task span: conversation.id = "uuid"
  → _call_agent(conversation_id=...) for each sub-agent
      → child agent.task span: conversation.id = "uuid"  ← same ID on all agents
```

This enables:
- **Conversations page** (Eval UI page 8) — browse sessions, see all turns, view per-conversation scores
- **Conversation filter** in Traces (page 2) and Prompt Lab X (page 7)
- **Multi-turn LLM judges** — run per `conversation_id`, scoring context retention, topic adherence, response consistency, conversation relevancy

---

## Setup

### 1. Prerequisites
- Python 3.11+
- The eval platform running (`make up` from repo root)
- An OpenAI API key

### 2. Create virtual environment

```bash
cd examples/opt-demo
python3 -m venv venv
source venv/bin/activate
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

Key dependencies:
- `google-adk[extensions]` — Google Agent Development Kit with LiteLLM
- `duckduckgo-search` — web search (no API key required)
- `fastapi` + `uvicorn` — backend API server
- `streamlit` — chat UI

### 4. Configure environment

```bash
cp .env.example .env
```

Edit `.env`:
```
OPENAI_API_KEY=sk-...your-key...
OPENAI_MODEL=gpt-4o-mini        # optional, default
```

### 5. Start the backend

```bash
uvicorn api:app --host 0.0.0.0 --port 8010 --workers 1
```

> **Use `--workers 1`** — multiple workers have separate memory and will cause duplicate eval rows due to in-memory dedup state not being shared between processes.

### 6. Start the chat UI (separate terminal)

```bash
source venv/bin/activate
streamlit run chat_ui.py
```

Opens at **http://localhost:8502**

---

## Chat UI

- **Multi-turn conversation** — session history persists across messages
- **New Conversation** — resets session, assigns a new `conversation_id`
- **Trace link** under every response → opens in Jaeger
- **Conversation ID** shown in sidebar
- **Latency** displayed per response

### Try These Prompts

**Simple search:**
```
Who is Rafael Nadal?
What are the latest developments in quantum computing?
```

**Search + summarize:**
```
Search for Serena Williams career and summarize in 20 words
Find information about the Eiffel Tower and summarize in 30 words
```

**Search + summarize + translate:**
```
Search for Roger Federer and summarize in 20 words and translate to Spanish
```

**Multi-turn (follow-ups):**
```
Who is Andre Agassi?
What were his Grand Slam titles?
Translate that to Hindi
```

---

## Observability After Running

### Traces in Jaeger
1. Open http://localhost:16686
2. Service: `multi-agent-demo2`
3. Each trace shows the full span tree:

```
agent.task  [orchestrator, root]
  ├── agent.task  [searcher]        ← conversation.id set here
  │     └── agent.tool_call  [web_search]
  ├── agent.task  [summarizer]      ← conversation.id set here
  └── agent.task  [translator]      ← conversation.id set here
```

### Eval Scores in Eval UI
1. Open http://localhost:8501
2. **Scores** page — scores per agent including multi-turn judges
3. **Conversations** page (page 8) — browse sessions by conversation_id
4. **Prompt Lab X** (page 7) — filter by conversation_id, see cost per trace

### LLM Spans
Google ADK + LiteLLM emit spans using **OTel GenAI semantic conventions**:
- `openai.chat` — OpenAI API call
- `call_llm` — LiteLLM wrapper
- `generate_content openai/gpt-4o-mini` — ADK internal

Token counts available on these spans: `gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`.

---

## Instrumentation Architecture

```
chat_ui.py  →  POST /chat  →  api.py
                                │
                                ▼
                         runner.run_agent(
                           user_input, user_id,
                           conversation_id=uuid
                         )
                                │
                         Opens agent.task span [orchestrator]
                         conversation.id = uuid
                                │
                         _classify_query(user_input)
                                │
                    ┌───────────┴───────────┐
                    │                       │
              pipeline?               passthrough?
                    │                       │
             _run_pipeline()         _call_agent(orchestrator,
                    │                  open_child_span=False)
          searcher → summarizer
            → translator
          each via _call_agent(
            open_child_span=True,
            conversation_id=uuid  ← propagated
          )
          each child span:
            agent.id = "searcher" etc.
            conversation.id = uuid  ← same on all
```

All spans export via OTLP HTTP → OTel Collector → ClickHouse + Jaeger + Eval Runner.

---

## File Structure

```
opt-demo/
├── .env                      Environment flags (API keys, model, Axis 3 settings)
├── requirements.txt          Python dependencies
│
├── demo_telemetry.py         OTel TracerProvider init (private provider for ADK compatibility)
│                             Exports: init_demo_telemetry(), get_tracer(), get_run_id()
│
├── api.py                    FastAPI backend
│                             POST /chat  → run_agent()
│                             POST /reset → reset_session()
│                             GET  /health
│
├── runner.py                 Core instrumentation + routing logic
│                             run_agent()        — root span + conversation.id
│                             _run_async()       — query classification + routing
│                             _run_pipeline()    — explicit search→summarize→translate
│                             _call_agent()      — child span with conversation.id
│                             _classify_query()  — rule-based intent classifier
│                             _slm_search/summarize/translate() — Axis 3 intercepts
│
├── chat_ui.py                Streamlit multi-turn chat interface
│
├── slm_server.py             Axis 3 FastAPI inference server (MPS GPU mode)
│                             POST /translate, /summarize, /synthesize, GET /health
│
├── start_slm_server.sh       Starts slm_server on port 8001 (set AXIS3_SLM_BACKEND=server)
│
├── agents/
│   ├── __init__.py           Factory function exports
│   └── orchestrator.py       All four LlmAgent definitions + orchestrator
│
├── tools/
│   └── search.py             DuckDuckGo web_search(query) tool
│
└── axis3/                    Axis 3 — SLM Specialization
    ├── translator_slm.py     NLLB-200: translate() — local or remote dispatch
    ├── summarizer_slm.py     BART-large-cnn: summarize() — local or remote dispatch
    ├── search_slm.py         Qwen3-0.6B: synthesize() — local or remote dispatch
    └── confidence.py         Per-task confidence scoring for escalation gating
```

---

## Troubleshooting

**`OPENAI_API_KEY not set`**
→ Create `.env` from `.env.example` and add your key.

**`No module named 'google.adk'`**
→ Activate venv: `source venv/bin/activate`, then `pip install -r requirements.txt`

**Traces not appearing in Jaeger**
→ Check platform is running: `make status` from repo root.
→ Check collector: http://localhost:8888/metrics

**Seeing duplicate rows in Prompt Lab X**
→ Ensure backend runs with `--workers 1` (not 2+).
→ Duplicates from before the fix can't be undone — they're already in ClickHouse.

**`conversation.id` missing on sub-agent spans**
→ Ensure you're running the latest `runner.py` — `_call_agent()` must accept and set `conversation_id`.
→ Only new sessions after the fix will have it propagated.

**DuckDuckGo rate limit errors**
→ Wait 30 seconds and retry.

**`Task was destroyed but it is pending` in logs**
→ LiteLLM internal logging task abandoned when the per-request event loop closes. Harmless — does not affect spans or eval data.

**`Event from an unknown agent` in logs**
→ ADK session events from a previous request arriving after a new request starts. Harmless.

**SLM server connection refused (`AXIS3_SLM_BACKEND=server`)**
→ Start the server first: `./start_slm_server.sh`. Wait for `All models ready on mps` before sending requests.

**SLM server slow on first startup**
→ Models are downloading or loading from disk. NLLB + BART + Qwen3 total ~4 GB. Subsequent startups load from `~/.cache/huggingface/hub/` and are faster.

**SLM outputs 4× inflated token counts in Prompt Analysis**
→ Ensure eval-runner image is rebuilt after any eval_runner code change: `docker compose build eval-runner && docker compose up -d eval-runner`. See `docs/slm-hf-models.md` for root cause details.
