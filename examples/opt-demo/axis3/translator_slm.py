"""
Axis 3 — Translator SLM: NLLB-200 (facebook/nllb-200-distilled-600M)

Option A: purpose-built model, no training needed.
Loaded once at first call and kept in memory.

BCP-47 codes required by NLLB-200 (not ISO 639-1):
  eng_Latn, fra_Latn, deu_Latn, spa_Latn, hin_Deva,
  jpn_Jpan, zho_Hans, arb_Arab, por_Latn, ita_Latn, etc.

Backend selection (AXIS3_SLM_BACKEND env var):
  local  — in-process CPU inference (default)
  server — POST to SLM server at AXIS3_SLM_SERVER_URL (MPS GPU)
"""

from __future__ import annotations
import os
import torch

MODEL_ID    = "facebook/nllb-200-distilled-600M"
PARAMS_B    = 0.6
THRESHOLD   = 0.85

# BCP-47 map — lowercase common names → NLLB code
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


def _resolve_lang(language: str) -> str | None:
    return _LANG_MAP.get(language.lower().strip())


def _translate_remote(text: str, target_language: str) -> tuple[str, int, int]:
    import httpx
    resp = httpx.post(
        f"{_SERVER_URL}/translate",
        json={"text": text, "target_language": target_language},
        timeout=120,
    )
    resp.raise_for_status()
    d = resp.json()
    return d["text"], d["input_tokens"], d["output_tokens"]


def translate(text: str, target_language: str) -> tuple[str, int, int]:
    """
    Translate text into target_language using NLLB-200.

    Returns (translated_text, input_tokens, output_tokens).
    Raises ValueError if the target language is not in the BCP-47 map.
    """
    if _BACKEND == "server":
        return _translate_remote(text, target_language)

    _load()

    tgt_code = _resolve_lang(target_language)
    if tgt_code is None:
        raise ValueError(
            f"Language '{target_language}' not in NLLB-200 BCP-47 map. "
            f"Supported: {sorted(_LANG_MAP.keys())}"
        )

    inputs = _tokenizer(text, return_tensors="pt", truncation=True, max_length=512)
    in_tokens = inputs["input_ids"].shape[1]

    forced_bos = _tokenizer.convert_tokens_to_ids(tgt_code)

    with torch.no_grad():
        output_ids = _model.generate(
            **inputs,
            forced_bos_token_id=forced_bos,
            max_length=256,
            num_beams=4,
        )

    out_tokens = output_ids.shape[1]
    translated = _tokenizer.decode(output_ids[0], skip_special_tokens=True)

    return translated, in_tokens, out_tokens
