"""
Per-task confidence scoring for Axis 3 SLM outputs.
Returns a float 0.0–1.0. Scores below the agent's threshold trigger
escalation to the gpt-4o-mini fallback path.
"""

from __future__ import annotations


def score_translation(output: str, source: str, target_language: str) -> float:
    """
    Score a translation output.
    Checks: non-empty, differs from source, plausible length, correct language.
    """
    if not output or not output.strip():
        return 0.0

    out = output.strip()

    # Must not be identical to the source
    if out.lower() == source.strip().lower():
        return 0.1

    # Length ratio: translation should be 0.5–2.5× the source length
    src_len = max(len(source.strip()), 1)
    ratio = len(out) / src_len
    if ratio < 0.3 or ratio > 3.5:
        return 0.3

    # Language detection check (best-effort — fail open if unavailable)
    try:
        from langdetect import detect
        lang_map = {
            "french": "fr", "spanish": "es", "german": "de",
            "hindi": "hi", "japanese": "ja", "chinese": "zh-cn",
            "arabic": "ar", "portuguese": "pt", "italian": "it",
            "russian": "ru", "korean": "ko", "dutch": "nl",
            "turkish": "tr", "polish": "pl", "swedish": "sv",
        }
        expected_code = lang_map.get(target_language.lower())
        if expected_code:
            detected = detect(out)
            if detected != expected_code:
                return 0.45
    except Exception:
        pass  # langdetect unavailable or ambiguous — don't penalise

    return 0.92


def score_summary(output: str, requested_word_count: int) -> float:
    """
    Score a summarization output.
    Checks: non-empty, shorter than a reasonable max, word count within ±35% of request.
    """
    if not output or not output.strip():
        return 0.0

    out = output.strip()
    actual_words = len(out.split())

    if actual_words == 0:
        return 0.0

    # Word count tolerance: ±35% of requested
    low  = requested_word_count * 0.65
    high = requested_word_count * 1.35

    if low <= actual_words <= high:
        return 0.88

    # Partially penalise — further from target → lower score
    deviation = abs(actual_words - requested_word_count) / max(requested_word_count, 1)
    if deviation < 0.6:
        return 0.60

    return 0.30


def score_search_synthesis(output: str, raw_results: str) -> float:
    """
    Score a search synthesis output.
    Checks: non-empty, references content from raw results, not a refusal.
    """
    if not output or not output.strip():
        return 0.0

    out = output.strip().lower()

    # Refusal / error patterns
    refusal_phrases = [
        "i cannot", "i'm unable", "i don't have access",
        "no results", "search failed", "i don't know",
    ]
    if any(p in out for p in refusal_phrases):
        return 0.2

    # Minimum length — synthesis should produce something substantive
    if len(out.split()) < 20:
        return 0.35

    # Check that the output references at least some content from raw results
    # (simple word overlap check)
    raw_words = set(raw_results.lower().split())
    out_words  = set(out.split())
    # Remove stopwords approximation — words > 4 chars
    raw_content = {w for w in raw_words  if len(w) > 4}
    out_content = {w for w in out_words  if len(w) > 4}

    if not raw_content:
        return 0.70  # no raw results to check against

    overlap = len(raw_content & out_content) / len(raw_content)
    if overlap < 0.05:
        return 0.40  # output ignores the search results entirely

    return 0.85
