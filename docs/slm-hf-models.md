# SLM Execution: HuggingFace Models in Axis 3

Axis 3 (SLM Specialization) replaces gpt-4o-mini for three specific tasks — translation, summarization, and search synthesis — with purpose-built small language models (SLMs) from HuggingFace. This document explains what each model does, where and how it runs, and what the operational implications are.

---

## Where the models run

All three SLMs run **locally on the host machine**, **in-process** within the opt-demo Streamlit Python process (`chat_ui.py`). They are:

- **Not** running inside Docker
- **Not** calling any external API
- **Not** a separate service or sidecar

When `AXIS3_SLM_ENABLED=true` in `.env` and the Streamlit app starts, models are loaded into the Python process on first use and kept resident in memory for the duration of the session. Inference happens on the **CPU** — no GPU required for any of the three models.

---

## Model weights: download and cache

Weights are downloaded automatically from HuggingFace Hub on first use via `transformers.AutoTokenizer.from_pretrained()` and `AutoModelForSeq2SeqLM.from_pretrained()`. They are cached locally at:

```
~/.cache/huggingface/hub/
```

Subsequent runs (same session or new session) load from cache without network access unless the model version changes. This means:

- **First run**: incurs a one-time download (see sizes below)
- **Subsequent runs**: load from disk, no internet required

---

## The three models

### 1. NLLB-200 — Translation
**File**: `axis3/translator_slm.py`  
**Model ID**: `facebook/nllb-200-distilled-600M`  
**Parameters**: 0.6B  
**Architecture**: Seq2Seq (encoder-decoder)

NLLB-200 (No Language Left Behind) is a purpose-built multilingual translation model trained by Meta. The distilled 600M variant supports 200+ languages using BCP-47 language codes rather than ISO 639-1 codes.

**How it works at runtime:**
1. On first call, loads tokenizer + model into memory (`_load()` — lazy, runs once per process)
2. Tokenizes the source text (max 512 tokens; longer inputs are truncated)
3. Runs beam search (`num_beams=4`) to generate the translation
4. Decodes output tokens back to text, skipping special tokens

**Language codes used** (not ISO names):

| Common name | NLLB BCP-47 code |
|-------------|------------------|
| French      | `fra_Latn`       |
| Hindi       | `hin_Deva`       |
| Spanish     | `spa_Latn`       |
| German      | `deu_Latn`       |
| Japanese    | `jpn_Jpan`       |
| Chinese     | `zho_Hans`       |
| Arabic      | `arb_Arab`       |

**Confidence threshold**: 0.85. The confidence scorer (`confidence.score_translation()`) checks: output is non-empty, differs from source, length ratio is 0.5–2.5×, and detected language (via `langdetect`) matches the requested target. Scores below 0.85 trigger escalation to gpt-4o-mini.

**Approximate model size on disk**: ~1.1 GB

---

### 2. BART-large-cnn — Summarization
**File**: `axis3/summarizer_slm.py`  
**Model ID**: `facebook/bart-large-cnn`  
**Parameters**: ~0.4B (400M)  
**Architecture**: Seq2Seq (encoder-decoder), pre-trained on CNN/DailyMail news

BART-large-cnn is pre-trained specifically for abstractive news summarization and requires no fine-tuning for general-purpose summarization tasks.

**How it works at runtime:**
1. Lazy-loads tokenizer + model on first call
2. Tokenizes the source text (max 1024 tokens; truncated beyond that)
3. Target length is derived from the requested word count: `max_tokens = word_count × 1.5`, `min_tokens = word_count × 0.8`
4. Runs beam search (`num_beams=4`, `early_stopping=True`, `no_repeat_ngram_size=3`)
5. Decodes and returns the summary

**Word count approximation**: The model operates in token space, not word space. The conversion uses the approximation 1 word ≈ 1.35 tokens. Actual word count can vary ±35% from the target — outputs outside this tolerance score below the confidence threshold.

**Confidence threshold**: 0.75. The scorer (`confidence.score_summary()`) checks actual word count against requested word count within ±35% tolerance. Deviation beyond 60% of target scores 0.30 and triggers gpt-4o-mini fallback.

**Approximate model size on disk**: ~1.6 GB

---

### 3. Qwen3-0.6B — Search Synthesis
**File**: `axis3/search_slm.py`  
**Model ID**: `Qwen/Qwen3-0.6B`  
**Parameters**: 0.6B  
**Architecture**: Decoder-only causal LM (like GPT)

Qwen3-0.6B replaces gpt-4o-mini only for the **synthesis step** — turning raw DuckDuckGo search results into a readable answer. The `web_search()` tool call itself is unchanged (still a Python DDG API call).

**How it works at runtime:**
1. Lazy-loads tokenizer + causal LM on first call (`AutoModelForCausalLM`, loaded as `float32` on CPU)
2. Formats raw search results into a structured prompt (truncated to 3000 chars)
3. Generates up to 300 new tokens (`do_sample=False` — greedy/deterministic)
4. Decodes only the newly generated tokens (strips the prompt prefix from output IDs)

**Key difference from NLLB/BART**: Qwen3-0.6B is a causal (decoder-only) model. It generates tokens autoregressively — each new token attends to all prior tokens. The other two are encoder-decoder models that encode the entire input first, then decode. This makes Qwen3 slightly slower per token for longer contexts.

**Confidence threshold**: 0.80. The scorer (`confidence.score_search_synthesis()`) checks: non-empty, no refusal phrases ("I cannot", "I'm unable", etc.), minimum 20 words, and ≥5% word overlap with the raw search results. The overlap check ensures the model actually used the search content rather than hallucinating.

**Approximate model size on disk**: ~1.2 GB

---

## Memory and loading behavior

All three modules use the same lazy-load pattern — module-level globals `_tokenizer` and `_model` initialized to `None`, populated on the first call to `_load()`:

```python
_tokenizer = None
_model     = None

def _load():
    global _tokenizer, _model
    if _tokenizer is None:
        from transformers import AutoTokenizer, AutoModelFor...
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        _model     = AutoModelFor....from_pretrained(MODEL_ID)
        _model.eval()
```

**Implications:**
- Models load once per process, on the first request that needs them
- They stay resident for the lifetime of the Streamlit session
- If `AXIS3_SLM_ENABLED=false`, `_load()` is never called, and no weights are loaded
- Multiple concurrent Streamlit sessions (multiple browser tabs / users) each load their own copy into separate Python processes — RAM usage multiplies accordingly
- Restarting Streamlit frees the memory and reloads models on next use

---

## Confidence-gated fallback

Each SLM call is followed by a confidence score check. If the score falls below the model's threshold, the agent automatically re-runs the same task using gpt-4o-mini. The thresholds:

| Model       | Task        | Threshold |
|-------------|-------------|-----------|
| NLLB-200    | Translation | 0.85      |
| BART-large  | Summary     | 0.75      |
| Qwen3-0.6B  | Search      | 0.80      |

The fallback is silent to the user — the eval pipeline records both paths in span attributes so escalation rates are visible in the Eval Measurements UI.

---

## Enabling / disabling SLM mode

Controlled by a single flag in `examples/opt-demo/.env`:

```bash
AXIS3_SLM_ENABLED=true   # Use SLMs with gpt-4o-mini fallback
AXIS3_SLM_ENABLED=false  # Use gpt-4o-mini directly for all three tasks
```

After changing this flag, restart the Streamlit app (`chat_ui.py`) — no Docker rebuild required since the opt-demo runs directly on the host.

---

## SLM backend: local in-process vs GPU server

When `AXIS3_SLM_ENABLED=true` you can choose how inference runs via a second flag:

| `AXIS3_SLM_BACKEND` | Inference location | Device | Cold-start |
|---|---|---|---|
| `local` (default) | In-process within Streamlit | CPU | On first request per session |
| `server` | Separate FastAPI process | MPS GPU (Apple Silicon) | At server startup, zero per-request |

### Local mode (default)

No extra steps. Models lazy-load into the Streamlit process on first use and stay resident for the session lifetime. Multiple Streamlit sessions each load their own copy. CPU-only.

### Server mode (MPS GPU)

The SLM server (`slm_server.py`) is a persistent FastAPI process that loads all three models once at startup onto the MPS device (Apple Silicon GPU) and serves them over HTTP on port 8001. The Streamlit process POSTs requests to it instead of running inference in-process.

**Start the server before launching Streamlit:**

```bash
cd examples/opt-demo
./start_slm_server.sh
```

Wait for the log line `All models ready on mps` before sending requests. Then in `.env`:

```bash
AXIS3_SLM_BACKEND=server
AXIS3_SLM_SERVER_URL=http://localhost:8001   # default, change if running remotely
```

Restart Streamlit after changing the backend flag. The server keeps running independently — Streamlit restarts do not reload the models.

**Check server health:**

```bash
curl http://localhost:8001/health
# {"status": "ready", "device": "mps"}
```

### Concurrency model

The server uses a `ThreadPoolExecutor(max_workers=3)` — one thread slot per model. Each model has its own `threading.Lock`:

- Three simultaneous requests (one per model) run in parallel on the GPU.
- Two requests for the same model queue behind the lock; the second waits for the first to finish.
- PyTorch releases the GIL during C++ inference, so threads don't block each other across models.

### Why server mode is faster

- **MPS GPU acceleration**: 3–6× throughput over CPU for the generation loop, most noticeable on BART (summarization) and Qwen3 (search synthesis) which do longer autoregressive decoding.
- **No cold-start latency**: models are already in GPU memory when the first request arrives.
- **Single copy in RAM**: one server process regardless of how many Streamlit sessions are open.
- **Qwen3 float16**: the server loads Qwen3-0.6B in `float16` on MPS (vs `float32` in local mode), halving memory bandwidth pressure with negligible quality change at 0.6B scale.

### Device fallback

If MPS is not available (non-Apple-Silicon machine), the server automatically falls back to CPU:

```python
_device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")
```

The server logs the selected device at startup.

### Comparing both backends

To benchmark local vs server for the same query:

1. Run with `AXIS3_SLM_BACKEND=local`, note wall-clock time in the Streamlit UI.
2. Start `./start_slm_server.sh`, switch to `AXIS3_SLM_BACKEND=server`, restart Streamlit, run the same query.
3. The Eval Measurements UI records `ios.axis3.model_used` and token counts for both runs — trace timestamps in Jaeger show end-to-end latency per agent task span.
