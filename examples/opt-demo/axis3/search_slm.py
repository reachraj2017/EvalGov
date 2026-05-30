"""
Axis 3 — Search SLM: Qwen3-0.6B synthesis

The web_search() tool is called directly in Python (unchanged).
Qwen3-0.6B replaces gpt-4o-mini only for the synthesis step:
  raw DDG results → readable answer.

Model is loaded on first call and kept in memory.

Backend selection (AXIS3_SLM_BACKEND env var):
  local  — in-process CPU inference (default)
  server — POST to SLM server at AXIS3_SLM_SERVER_URL (MPS GPU)
"""

from __future__ import annotations
import os
import torch

MODEL_ID  = "Qwen/Qwen3-0.6B"
PARAMS_B  = 0.6
THRESHOLD = 0.80

_BACKEND    = os.getenv("AXIS3_SLM_BACKEND", "local").lower()
_SERVER_URL = os.getenv("AXIS3_SLM_SERVER_URL", "http://localhost:8001")

_tokenizer = None
_model     = None


def _synthesize_remote(query: str, raw_results: str) -> tuple[str, int, int]:
    import httpx
    resp = httpx.post(
        f"{_SERVER_URL}/synthesize",
        json={"query": query, "raw_results": raw_results},
        timeout=120,
    )
    resp.raise_for_status()
    d = resp.json()
    return d["text"], d["input_tokens"], d["output_tokens"]


def _load():
    global _tokenizer, _model
    if _tokenizer is None:
        from transformers import AutoTokenizer, AutoModelForCausalLM
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        if _tokenizer.pad_token_id is None:
            _tokenizer.pad_token_id = _tokenizer.eos_token_id
        _model     = AutoModelForCausalLM.from_pretrained(MODEL_ID, dtype=torch.float32)
        _model.eval()


_SYNTHESIS_PROMPT = """\
You are a helpful assistant. Based on the following web search results, \
write a clear and accurate answer to the user's query.
Be concise, factual, and cite 2-3 sources at the end.

Search query: {query}

Search results:
{results}

Answer:"""


def synthesize(query: str, raw_results: str) -> tuple[str, int, int]:
    """
    Synthesize web search results into a readable answer using Qwen3-0.6B.

    Returns (answer_text, input_tokens, output_tokens).
    """
    if _BACKEND == "server":
        return _synthesize_remote(query, raw_results)

    _load()

    prompt = _SYNTHESIS_PROMPT.format(
        query=query,
        results=raw_results[:3000],
    )

    inputs = _tokenizer(prompt, return_tensors="pt", truncation=True, max_length=2048)
    in_tokens = inputs["input_ids"].shape[1]

    with torch.no_grad():
        output_ids = _model.generate(
            inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            max_new_tokens=300,
            do_sample=False,
            pad_token_id=_tokenizer.pad_token_id,
        )

    out_tokens = output_ids.shape[1] - in_tokens
    # Decode only the newly generated tokens
    answer = _tokenizer.decode(
        output_ids[0][in_tokens:],
        skip_special_tokens=True,
    ).strip()

    return answer, in_tokens, max(out_tokens, 0)
