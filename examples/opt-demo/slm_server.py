"""
SLM Server — GPU-accelerated HuggingFace inference over HTTP.

Loads all three Axis 3 models once at startup on the best available device
(MPS on Apple Silicon, otherwise CPU) and serves them as a persistent FastAPI
process.  The Streamlit / runner process POSTs requests here instead of
running inference in-process.

Endpoints:
    POST /translate  {"text": "...", "target_language": "French"}
    POST /summarize  {"text": "...", "word_count": 25}
    POST /synthesize {"query": "...", "raw_results": "..."}
    GET  /health     → {"status": "ready", "device": "mps"}

Start:
    ./start_slm_server.sh
or:
    uvicorn slm_server:app --host 0.0.0.0 --port 8001 --workers 1
"""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

import torch
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# ── Device ─────────────────────────────────────────────────────────────────────
_device = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

# ── Model handles (populated at startup) ───────────────────────────────────────
_translate_tokenizer  = None
_translate_model      = None
_translate_lock       = threading.Lock()

_summarize_tokenizer  = None
_summarize_model      = None
_summarize_lock       = threading.Lock()

_synthesize_tokenizer = None
_synthesize_model     = None
_synthesize_lock      = threading.Lock()

# One thread per model — allows NLLB, BART, Qwen3 to run concurrently
# when requests arrive simultaneously; same-model requests queue behind the lock.
_executor = ThreadPoolExecutor(max_workers=3)

# ── NLLB-200 BCP-47 language map ───────────────────────────────────────────────
_LANG_MAP: dict[str, str] = {
    "french":     "fra_Latn",
    "spanish":    "spa_Latn",
    "german":     "deu_Latn",
    "hindi":      "hin_Deva",
    "japanese":   "jpn_Jpan",
    "chinese":    "zho_Hans",
    "arabic":     "arb_Arab",
    "portuguese": "por_Latn",
    "italian":    "ita_Latn",
    "russian":    "rus_Cyrl",
    "korean":     "kor_Hang",
    "dutch":      "nld_Latn",
    "turkish":    "tur_Latn",
    "polish":     "pol_Latn",
    "swedish":    "swe_Latn",
    "english":    "eng_Latn",
}

_SYNTHESIS_PROMPT = """\
You are a helpful assistant. Based on the following web search results, \
write a clear and accurate answer to the user's query.
Be concise, factual, and cite 2-3 sources at the end.

Search query: {query}

Search results:
{results}

Answer:"""


def _load_models() -> None:
    global _translate_tokenizer, _translate_model
    global _summarize_tokenizer, _summarize_model
    global _synthesize_tokenizer, _synthesize_model

    from transformers import AutoTokenizer, AutoModelForSeq2SeqLM, AutoModelForCausalLM

    print(f"[slm_server] Loading models on device: {_device}", flush=True)

    print("[slm_server] Loading NLLB-200 (translator)...", flush=True)
    _translate_tokenizer = AutoTokenizer.from_pretrained("facebook/nllb-200-distilled-600M")
    _translate_model = AutoModelForSeq2SeqLM.from_pretrained(
        "facebook/nllb-200-distilled-600M"
    ).to(_device)
    _translate_model.eval()

    print("[slm_server] Loading BART-large-cnn (summarizer)...", flush=True)
    _summarize_tokenizer = AutoTokenizer.from_pretrained("facebook/bart-large-cnn")
    _summarize_model = AutoModelForSeq2SeqLM.from_pretrained("facebook/bart-large-cnn").to(
        _device
    )
    _summarize_model.eval()

    print("[slm_server] Loading Qwen3-0.6B (search synthesizer)...", flush=True)
    # float16 halves MPS memory bandwidth vs float32 for negligible quality loss at 0.6B
    dtype = torch.float16 if _device.type == "mps" else torch.float32
    _synthesize_tokenizer = AutoTokenizer.from_pretrained("Qwen/Qwen3-0.6B")
    if _synthesize_tokenizer.pad_token_id is None:
        _synthesize_tokenizer.pad_token_id = _synthesize_tokenizer.eos_token_id
    _synthesize_model = AutoModelForCausalLM.from_pretrained(
        "Qwen/Qwen3-0.6B", torch_dtype=dtype
    ).to(_device)
    _synthesize_model.eval()

    print(f"[slm_server] All models ready on {_device}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _load_models)
    yield
    _executor.shutdown(wait=False)


app = FastAPI(title="SLM Server", version="1.0.0", lifespan=lifespan)


# ── Pydantic schemas ────────────────────────────────────────────────────────────

class TranslateRequest(BaseModel):
    text: str
    target_language: str

class SummarizeRequest(BaseModel):
    text: str
    word_count: int = 25

class SynthesizeRequest(BaseModel):
    query: str
    raw_results: str

class InferenceResponse(BaseModel):
    text: str
    input_tokens: int
    output_tokens: int


# ── Sync inference functions (run inside ThreadPoolExecutor) ───────────────────

def _do_translate(text: str, target_language: str) -> tuple[str, int, int]:
    tgt_code = _LANG_MAP.get(target_language.lower().strip())
    if tgt_code is None:
        raise ValueError(
            f"Language '{target_language}' not supported. "
            f"Supported: {sorted(_LANG_MAP.keys())}"
        )

    with _translate_lock:
        inputs = _translate_tokenizer(
            text, return_tensors="pt", truncation=True, max_length=512
        )
        in_tokens = inputs["input_ids"].shape[1]
        inputs = {k: v.to(_device) for k, v in inputs.items()}

        forced_bos = _translate_tokenizer.convert_tokens_to_ids(tgt_code)
        with torch.no_grad():
            output_ids = _translate_model.generate(
                **inputs,
                forced_bos_token_id=forced_bos,
                max_length=256,
                num_beams=4,
            )

    out_tokens = output_ids.shape[1]
    translated = _translate_tokenizer.decode(output_ids[0].cpu(), skip_special_tokens=True)
    return translated, in_tokens, out_tokens


def _do_summarize(text: str, word_count: int) -> tuple[str, int, int]:
    max_tok = max(int(word_count * 1.5), 10)
    min_tok = max(int(word_count * 0.8),  5)

    with _summarize_lock:
        inputs = _summarize_tokenizer(
            text, return_tensors="pt", truncation=True, max_length=1024
        )
        in_tokens = inputs["input_ids"].shape[1]
        input_ids = inputs["input_ids"].to(_device)

        with torch.no_grad():
            output_ids = _summarize_model.generate(
                input_ids,
                max_length=max_tok,
                min_length=min_tok,
                num_beams=4,
                early_stopping=True,
                no_repeat_ngram_size=3,
            )

    out_tokens = output_ids.shape[1]
    summary = _summarize_tokenizer.decode(output_ids[0].cpu(), skip_special_tokens=True)
    return summary, in_tokens, out_tokens


def _do_synthesize(query: str, raw_results: str) -> tuple[str, int, int]:
    prompt = _SYNTHESIS_PROMPT.format(query=query, results=raw_results[:3000])

    with _synthesize_lock:
        inputs = _synthesize_tokenizer(
            prompt, return_tensors="pt", truncation=True, max_length=2048
        )
        in_tokens = inputs["input_ids"].shape[1]
        inputs = {k: v.to(_device) for k, v in inputs.items()}

        with torch.no_grad():
            output_ids = _synthesize_model.generate(
                inputs["input_ids"],
                attention_mask=inputs["attention_mask"],
                max_new_tokens=300,
                do_sample=False,
                pad_token_id=_synthesize_tokenizer.pad_token_id,
            )

    out_tokens = max(output_ids.shape[1] - in_tokens, 0)
    answer = _synthesize_tokenizer.decode(
        output_ids[0][in_tokens:].cpu(), skip_special_tokens=True
    ).strip()
    return answer, in_tokens, out_tokens


# ── Endpoints ──────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ready", "device": str(_device)}


@app.post("/translate", response_model=InferenceResponse)
async def translate(req: TranslateRequest):
    loop = asyncio.get_event_loop()
    try:
        text, in_tok, out_tok = await loop.run_in_executor(
            _executor, _do_translate, req.text, req.target_language
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    return InferenceResponse(text=text, input_tokens=in_tok, output_tokens=out_tok)


@app.post("/summarize", response_model=InferenceResponse)
async def summarize(req: SummarizeRequest):
    loop = asyncio.get_event_loop()
    text, in_tok, out_tok = await loop.run_in_executor(
        _executor, _do_summarize, req.text, req.word_count
    )
    return InferenceResponse(text=text, input_tokens=in_tok, output_tokens=out_tok)


@app.post("/synthesize", response_model=InferenceResponse)
async def synthesize(req: SynthesizeRequest):
    loop = asyncio.get_event_loop()
    text, in_tok, out_tok = await loop.run_in_executor(
        _executor, _do_synthesize, req.query, req.raw_results
    )
    return InferenceResponse(text=text, input_tokens=in_tok, output_tokens=out_tok)
