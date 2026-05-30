"""
Axis 3 — Summarizer SLM: BART-large-cnn (facebook/bart-large-cnn)

Option A: pre-trained on news summarization, no training needed.
Loaded once at first call and kept in memory.

Word count is approximated via token bounds (1 word ≈ 1.35 tokens).
Exact word count is not guaranteed — use confidence.score_summary()
to gate outputs outside the ±35% tolerance.

Backend selection (AXIS3_SLM_BACKEND env var):
  local  — in-process CPU inference (default)
  server — POST to SLM server at AXIS3_SLM_SERVER_URL (MPS GPU)
"""

from __future__ import annotations
import os
import torch

MODEL_ID  = "facebook/bart-large-cnn"
PARAMS_B  = 0.4
THRESHOLD = 0.75

_BACKEND    = os.getenv("AXIS3_SLM_BACKEND", "local").lower()
_SERVER_URL = os.getenv("AXIS3_SLM_SERVER_URL", "http://localhost:8001")

_tokenizer = None
_model     = None


def _load():
    global _tokenizer, _model
    if _tokenizer is None:
        from transformers import AutoTokenizer, AutoModelForSeq2SeqLM
        _tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
        _model     = AutoModelForSeq2SeqLM.from_pretrained(MODEL_ID)
        _model.eval()


def _summarize_remote(text: str, word_count: int) -> tuple[str, int, int]:
    import httpx
    resp = httpx.post(
        f"{_SERVER_URL}/summarize",
        json={"text": text, "word_count": word_count},
        timeout=120,
    )
    resp.raise_for_status()
    d = resp.json()
    return d["text"], d["input_tokens"], d["output_tokens"]


def _word_count_to_tokens(word_count: int) -> tuple[int, int]:
    """Convert a target word count to (min_tokens, max_tokens)."""
    max_tok = max(int(word_count * 1.5), 10)
    min_tok = max(int(word_count * 0.8),  5)
    return min_tok, max_tok


def summarize(text: str, word_count: int = 25) -> tuple[str, int, int]:
    """
    Summarize text to approximately word_count words using BART-large-cnn.

    Returns (summary_text, input_tokens, output_tokens).
    """
    if _BACKEND == "server":
        return _summarize_remote(text, word_count)

    _load()

    inputs = _tokenizer(
        text,
        return_tensors="pt",
        truncation=True,
        max_length=1024,
    )
    in_tokens = inputs["input_ids"].shape[1]

    min_tok, max_tok = _word_count_to_tokens(word_count)

    with torch.no_grad():
        output_ids = _model.generate(
            inputs["input_ids"],
            max_length=max_tok,
            min_length=min_tok,
            num_beams=4,
            early_stopping=True,
            no_repeat_ngram_size=3,
        )

    out_tokens = output_ids.shape[1]
    summary    = _tokenizer.decode(output_ids[0], skip_special_tokens=True)

    return summary, in_tokens, out_tokens
