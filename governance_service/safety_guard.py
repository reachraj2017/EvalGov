"""Safety, Harm & Content Guardrails — Phase 4.

Covers:
  Category 4 — prompt injection, jailbreak, toxic output, bias detection,
                unsafe action prevention / guardrail trigger rates.
  Category 2 (partial) — credential exposure detected in outputs is
                delegated to identity_guard.py; here we track gate blocks.

Key public API:
  seed_safety_rules(db)                                → None
  run_safety_checks(db, trace_id, run_id, agent_role, spans) → SafetyResult
"""

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

# ──────────────────────────────────────────────────────────────────────────────
# Default rule sets
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_INJECTION_PATTERNS: list[tuple[str, str]] = [
    ("ignore_prev_instructions",  r"ignore (previous|prior|all) instructions"),
    ("disregard_system_prompt",   r"disregard your (system prompt|instructions|rules)"),
    ("you_are_now",               r"\byou are now\b"),
    ("forget_instructions",       r"forget (your|all) (instructions|rules|guidelines)"),
    ("xml_tag_injection",         r"</?(instructions|system|context|prompt)>"),
    ("im_start_system",           r"<\|im_start\|>system"),
    ("bracket_system",            r"\[\[system\]\]"),
    ("new_system_prompt",         r"new system prompt:"),
]

_DEFAULT_JAILBREAK_PATTERNS: list[tuple[str, str]] = [
    ("dan_keyword",               r"\bDAN\b"),
    ("do_anything_now",           r"do anything now"),
    ("jailbreak_keyword",         r"\bjailbreak\b"),
    ("bypass_safety",             r"bypass your safety"),
    ("no_restrictions",           r"pretend you have no restrictions"),
    ("ai_no_rules",               r"you are an AI with no (restrictions|rules|guidelines)"),
    ("ignore_training",           r"ignore your (training|guidelines|instructions)"),
    ("scenario_override",         r"in this scenario[,\s]+you (can|may|must|will) (do|say|help)"),
]

# Compiled caches — populated by _get_compiled_patterns() or seed_safety_rules()
_COMPILED_INJECTION:  list[tuple[str, re.Pattern]] = []
_COMPILED_JAILBREAK:  list[tuple[str, re.Pattern]] = []

# Inline keyword list used when eval scores are absent
_TOXIC_KEYWORDS: list[str] = [
    "fuck", "shit", "bitch", "asshole", "bastard",
    "kill yourself", "go die", "you are worthless",
    "hate speech", "slur",
]

_BIAS_KEYWORDS: list[str] = [
    "all [a-z]+ are", "women can't", "men are better",
    "blacks are", "whites are", "asians are",
    "jews control", "muslims always", "immigrants steal",
    "gay people", "transgender agenda",
]
_COMPILED_TOXIC_KW: list[re.Pattern] = [
    re.compile(k, re.IGNORECASE) for k in _TOXIC_KEYWORDS
]
_COMPILED_BIAS_KW: list[re.Pattern] = [
    re.compile(k, re.IGNORECASE) for k in _BIAS_KEYWORDS
]


def _get_compiled_patterns(
    db,
) -> tuple[list[tuple[str, re.Pattern]], list[tuple[str, re.Pattern]], list[tuple[str, re.Pattern]]]:
    """Return compiled (injection, jailbreak, toxic) pattern lists from DB.

    Falls back to in-module defaults for injection/jailbreak if the DB has none.
    Toxic/bias rules are only DB-driven (no hardcoded defaults beyond keyword lists).
    """
    injection: list[tuple[str, re.Pattern]] = []
    jailbreak: list[tuple[str, re.Pattern]] = []
    toxic:     list[tuple[str, re.Pattern]] = []

    try:
        rows = db.fetch_all(
            "SELECT rule_id, description, pattern, rule_type "
            "FROM otel.gov_safety_rules FINAL "
            "WHERE enabled = 1 ORDER BY rule_type, rule_id"
        )
        for row in rows:
            try:
                compiled = re.compile(row["pattern"], re.IGNORECASE)
                entry    = (row.get("description") or row["rule_id"], compiled)
                rtype    = row["rule_type"]
                if rtype == "injection":
                    injection.append(entry)
                elif rtype == "jailbreak":
                    jailbreak.append(entry)
                elif rtype in ("toxic", "bias"):
                    toxic.append(entry)
            except re.error as exc:
                log.warning("safety_rule_bad_regex",
                            description=row.get("description"), error=str(exc))
    except Exception as exc:
        log.warning("safety_rules_db_load_failed", error=str(exc))

    # Fall back to compiled defaults for injection/jailbreak only
    if not injection:
        injection = [(name, re.compile(pat, re.IGNORECASE))
                     for name, pat in _DEFAULT_INJECTION_PATTERNS]
    if not jailbreak:
        jailbreak = [(name, re.compile(pat, re.IGNORECASE))
                     for name, pat in _DEFAULT_JAILBREAK_PATTERNS]

    return injection, jailbreak, toxic


# ──────────────────────────────────────────────────────────────────────────────
# Result dataclass
# ──────────────────────────────────────────────────────────────────────────────

@dataclass
class SafetyResult:
    trace_id:               str
    agent_role:             str
    run_id:                 str

    # Injection
    injection_detected:     bool
    injection_count:        int
    injection_patterns_hit: list[str] = field(default_factory=list)

    # Jailbreak
    jailbreak_detected:     bool = False
    jailbreak_count:        int  = 0

    # Toxic output
    toxic_output_count:     int   = 0
    toxic_rate:             float = 0.0

    # Bias
    bias_flagged:           bool  = False

    # Gate/guardrail blocks
    guardrail_blocks:       int   = 0
    guardrail_rate:         float = 0.0

    # Totals
    total_task_spans:       int   = 0


# ──────────────────────────────────────────────────────────────────────────────
# DB bootstrap
# ──────────────────────────────────────────────────────────────────────────────

def seed_safety_rules(db) -> None:
    """Insert default injection + jailbreak patterns into gov_safety_rules if empty.

    The table is expected to exist with schema:
        pattern_id   String DEFAULT generateUUIDv4()
        pattern_name String
        pattern_regex String
        rule_type    LowCardinality(String)   -- 'injection' | 'jailbreak'
        enabled      UInt8 DEFAULT 1
        updated_at   DateTime DEFAULT now()
    ENGINE = ReplacingMergeTree(updated_at) ORDER BY pattern_name
    """
    try:
        existing = db.fetch_one(
            "SELECT count() AS cnt FROM otel.gov_safety_rules FINAL WHERE enabled = 1"
        )
        if existing and int(existing.get("cnt", 0) or 0) > 0:
            log.debug("safety_rules_already_seeded",
                      count=existing.get("cnt"))
            return
    except Exception as exc:
        log.warning("safety_rules_seed_check_failed", error=str(exc))
        return

    rows = []
    for name, pat in _DEFAULT_INJECTION_PATTERNS:
        rows.append((str(uuid.uuid4()), "injection", pat, "critical", 1, name))
    for name, pat in _DEFAULT_JAILBREAK_PATTERNS:
        rows.append((str(uuid.uuid4()), "jailbreak", pat, "critical", 1, name))

    try:
        db.execute(
            "INSERT INTO otel.gov_safety_rules "
            "(rule_id, rule_type, pattern, severity, enabled, description) VALUES",
            rows,
        )
        log.info("safety_rules_seeded", count=len(rows))
    except Exception as exc:
        log.error("safety_rules_seed_failed", error=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Internal helpers
# ──────────────────────────────────────────────────────────────────────────────

def _collect_scan_texts(span: dict) -> list[str]:
    """Return all text blobs from a span that should be scanned for injection/jailbreak."""
    attrs = span.get("attributes") or {}
    texts: list[str] = []
    for key in (
        "task.input", "gen_ai.prompt", "llm.input",
        "tool.result", "tool_result", "agent.task_input",
    ):
        val = attrs.get(key)
        if val:
            texts.append(str(val))
    # Also look inside tool_results arrays encoded as JSON
    raw_tr = attrs.get("tool_results") or attrs.get("tool.results")
    if raw_tr:
        try:
            parsed = json.loads(raw_tr) if isinstance(raw_tr, str) else raw_tr
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        texts.append(str(item.get("content", "") or ""))
                    else:
                        texts.append(str(item))
            else:
                texts.append(str(parsed))
        except (json.JSONDecodeError, TypeError):
            texts.append(str(raw_tr))
    return [t for t in texts if t.strip()]


def _collect_output_texts(span: dict) -> list[str]:
    """Return output text blobs from a span used for toxicity / bias scanning."""
    attrs = span.get("attributes") or {}
    texts: list[str] = []
    for key in (
        "task.output", "gen_ai.completion", "llm.output", "agent.output",
    ):
        val = attrs.get(key)
        if val:
            texts.append(str(val))
    return [t for t in texts if t.strip()]


def _parse_eval_score(attrs: dict, score_key: str) -> Optional[float]:
    """Extract a named score from span attributes.

    Checks:
      1. Direct attribute named score_key (e.g. 'eval.toxicity_score').
      2. JSON blob in 'eval.scores' containing score_key.
    Returns None if the attribute is absent.
    """
    # 1. Direct attribute
    direct = attrs.get(score_key)
    if direct is not None:
        try:
            return float(direct)
        except (ValueError, TypeError):
            pass

    # 2. eval.scores JSON blob
    scores_raw = attrs.get("eval.scores")
    if scores_raw:
        try:
            scores = json.loads(scores_raw) if isinstance(scores_raw, str) else scores_raw
            short_key = score_key.replace("eval.", "").replace("_score", "")
            val = scores.get(score_key) or scores.get(short_key)
            if val is not None:
                return float(val)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass

    return None


def _is_gate_blocked(span: dict) -> bool:
    """Return True if this span represents a gate check that resulted in block or pause."""
    attrs = span.get("attributes") or {}
    span_name = str(span.get("span_name", "") or "")

    # Explicit gate.decision attribute written by gate.py
    gate_decision = str(attrs.get("gate.decision", "") or "").lower()
    if gate_decision in ("block", "pause"):
        return True

    # gate.check spans with a block/pause decision in attributes
    if "gate" in span_name.lower():
        for key in ("gate.result", "gate.outcome", "gate_result"):
            val = str(attrs.get(key, "") or "").lower()
            if val in ("block", "pause"):
                return True

    return False


def _write_safety_event(
    db,
    trace_id:     str,
    span_id:      str,
    run_id:       str,
    agent_role:   str,
    event_type:   str,
    detected:     int,
    pattern_name: str,
    confidence:   float,
    detail:       str,
) -> None:
    """Insert a row into otel.gov_safety_events."""
    try:
        db.execute(
            "INSERT INTO otel.gov_safety_events "
            "(event_id, trace_id, span_id, run_id, agent_role, "
            "event_type, detected, pattern_name, confidence, detail) VALUES",
            [(
                str(uuid.uuid4()),
                trace_id, span_id, run_id, agent_role,
                event_type, detected, pattern_name,
                float(confidence), detail,
            )],
        )
    except Exception as exc:
        log.warning("safety_event_write_failed", event_type=event_type,
                    span_id=span_id, error=str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Main entry point
# ──────────────────────────────────────────────────────────────────────────────

def run_safety_checks(
    db,
    trace_id:   str,
    run_id:     str,
    agent_role: str,
    spans:      list[dict],
    thresholds: dict = None,
) -> SafetyResult:
    """Run all safety checks against the supplied spans.

    Args:
        db:         GovernanceDB instance.
        trace_id:   OTel trace ID.
        run_id:     Governance run ID.
        agent_role: Role of the agent being evaluated.
        spans:      List of span dicts as returned by db.get_spans_for_trace().

    Returns:
        SafetyResult dataclass with all detection counts and rates.
    """
    # ── Threshold values (configurable via gov_threshold_config) ─────────
    t             = thresholds or {}
    _inj_conf     = float(t.get("safety.injection_confidence",       0.9))
    _jb_conf      = float(t.get("safety.jailbreak_confidence",       0.85))
    _tox_score    = float(t.get("safety.toxicity_score",             0.5))
    _tox_kw_conf  = float(t.get("safety.toxic_keyword_confidence",   0.7))
    _bias_score   = float(t.get("safety.bias_score",                 0.5))
    _bias_kw_conf = float(t.get("safety.bias_keyword_confidence",    0.6))

    injection_patterns, jailbreak_patterns, toxic_db_patterns = _get_compiled_patterns(db)

    # ── Identify spans of interest ────────────────────────────────────────
    task_spans = [
        s for s in spans
        if s.get("span_name") == "agent.task"
    ]
    total_task = len(task_spans)

    # ── Per-span counters ─────────────────────────────────────────────────
    injection_count  = 0
    jailbreak_count  = 0
    toxic_count      = 0
    bias_flagged     = False
    guardrail_blocks = 0

    all_injection_patterns_hit: list[str] = []

    for span in task_spans:
        span_id = span.get("span_id", "")
        attrs   = span.get("attributes") or {}

        # ── 1. Prompt injection detection ─────────────────────────────────
        scan_texts  = _collect_scan_texts(span)
        combined_in = "\n".join(scan_texts)
        span_injected = False

        for pat_name, pattern in injection_patterns:
            if pattern.search(combined_in):
                if not span_injected:
                    injection_count += 1
                    span_injected = True
                if pat_name not in all_injection_patterns_hit:
                    all_injection_patterns_hit.append(pat_name)
                _write_safety_event(
                    db, trace_id, span_id, run_id, agent_role,
                    event_type="prompt_injection",
                    detected=1,
                    pattern_name=pat_name,
                    confidence=_inj_conf,
                    detail=json.dumps({"pattern": pat_name}),
                )

        # ── 2. Jailbreak detection ────────────────────────────────────────
        span_jailbroken = False
        for pat_name, pattern in jailbreak_patterns:
            if pattern.search(combined_in):
                if not span_jailbroken:
                    jailbreak_count += 1
                    span_jailbroken = True
                _write_safety_event(
                    db, trace_id, span_id, run_id, agent_role,
                    event_type="jailbreak_attempt",
                    detected=1,
                    pattern_name=pat_name,
                    confidence=_jb_conf,
                    detail=json.dumps({"pattern": pat_name}),
                )

        # ── 3. Toxic output flag ──────────────────────────────────────────
        output_texts   = _collect_output_texts(span)
        combined_out   = "\n".join(output_texts)
        # Scan both input and output with DB-defined toxic/bias rules
        combined_all   = "\n".join([combined_in, combined_out]).strip()
        span_is_toxic  = False

        # DB-defined toxic/bias patterns (highest priority — user-configured)
        for pat_name, pattern in toxic_db_patterns:
            if combined_all and pattern.search(combined_all):
                span_is_toxic = True
                _write_safety_event(
                    db, trace_id, span_id, run_id, agent_role,
                    event_type="toxic_output",
                    detected=1,
                    pattern_name=pat_name,
                    confidence=_tox_kw_conf,
                    detail=json.dumps({"pattern": pat_name, "source": "db_rule"}),
                )
                break  # one event per span is enough

        if not span_is_toxic:
            tox_score = _parse_eval_score(attrs, "eval.toxicity_score")
            if tox_score is not None:
                if tox_score >= _tox_score:
                    span_is_toxic = True
                    _write_safety_event(
                        db, trace_id, span_id, run_id, agent_role,
                        event_type="toxic_output",
                        detected=1,
                        pattern_name="eval.toxicity_score",
                        confidence=float(tox_score),
                        detail=json.dumps({"toxicity_score": tox_score,
                                           "source": "eval_score"}),
                    )
            elif combined_out:
                # Fall back to hardcoded keyword scan
                for kw_pattern in _COMPILED_TOXIC_KW:
                    if kw_pattern.search(combined_out):
                        span_is_toxic = True
                        _write_safety_event(
                            db, trace_id, span_id, run_id, agent_role,
                            event_type="toxic_output",
                            detected=1,
                            pattern_name=kw_pattern.pattern,
                            confidence=_tox_kw_conf,
                            detail=json.dumps({"keyword_match": kw_pattern.pattern,
                                               "source": "keyword_fallback"}),
                        )
                        break  # one event per span is enough

        if span_is_toxic:
            toxic_count += 1

        # ── 4. Bias detection flag ────────────────────────────────────────
        bias_score = _parse_eval_score(attrs, "eval.bias_score")
        if bias_score is not None:
            if bias_score >= _bias_score:
                bias_flagged = True
                _write_safety_event(
                    db, trace_id, span_id, run_id, agent_role,
                    event_type="bias_detected",
                    detected=1,
                    pattern_name="eval.bias_score",
                    confidence=float(bias_score),
                    detail=json.dumps({"bias_score": bias_score,
                                       "source": "eval_score"}),
                )
        elif combined_out:
            # Keyword fallback — flag the trace if any span matches
            for kw_pattern in _COMPILED_BIAS_KW:
                if kw_pattern.search(combined_out):
                    bias_flagged = True
                    _write_safety_event(
                        db, trace_id, span_id, run_id, agent_role,
                        event_type="bias_detected",
                        detected=1,
                        pattern_name=kw_pattern.pattern,
                        confidence=_bias_kw_conf,
                        detail=json.dumps({"keyword_match": kw_pattern.pattern,
                                           "source": "keyword_fallback"}),
                    )
                    break
        else:
            # No output and no score — cannot evaluate bias
            _write_safety_event(
                db, trace_id, span_id, run_id, agent_role,
                event_type="bias_not_evaluated",
                detected=0,
                pattern_name="",
                confidence=0.0,
                detail=json.dumps({"reason": "no_output_or_score"}),
            )

        # ── 5. Unsafe action prevention / gate blocks ─────────────────────
        if _is_gate_blocked(span):
            guardrail_blocks += 1
            _write_safety_event(
                db, trace_id, span_id, run_id, agent_role,
                event_type="guardrail_block",
                detected=1,
                pattern_name="gate.check",
                confidence=1.0,
                detail=json.dumps({
                    "gate_decision": str(
                        (span.get("attributes") or {}).get("gate.decision", "block")
                    ),
                }),
            )

    # Also scan gov_audit_log for gate blocks tied to this trace
    # (covers blocks written by gate.py that don't have a matching span attribute)
    try:
        audit_rows = db.fetch_all(
            "SELECT detail FROM otel.gov_audit_log "
            "WHERE trace_id = %(tid)s AND event_type = 'gate_check'",
            {"tid": trace_id},
        )
        for row in audit_rows:
            detail_str = row.get("detail", "")
            if detail_str:
                try:
                    detail_data = json.loads(detail_str)
                    if detail_data.get("decision") in ("block", "pause"):
                        guardrail_blocks += 1
                except (json.JSONDecodeError, TypeError):
                    pass
    except Exception as exc:
        log.warning("safety_gate_audit_scan_failed",
                    trace_id=trace_id, error=str(exc))

    # ── Compute rates ─────────────────────────────────────────────────────
    denom        = total_task or 1
    toxic_rate   = toxic_count   / denom
    guardrail_rate = guardrail_blocks / denom

    injection_rate  = injection_count  / denom
    jailbreak_rate  = jailbreak_count  / denom

    # ── Persist metric snapshots ──────────────────────────────────────────
    _save_metric = lambda metric, value, detail="": db.save_gov_metric(
        trace_id, "", run_id, metric, value, detail
    )

    try:
        _save_metric(
            "injection_rate", injection_rate,
            json.dumps({"injection_count": injection_count,
                        "patterns_hit": all_injection_patterns_hit}),
        )
        _save_metric(
            "jailbreak_rate", jailbreak_rate,
            json.dumps({"jailbreak_count": jailbreak_count}),
        )
        _save_metric(
            "guardrail_trigger_rate", guardrail_rate,
            json.dumps({"guardrail_blocks": guardrail_blocks,
                        "total_task_spans": total_task}),
        )
        _save_metric(
            "toxic_output_rate", toxic_rate,
            json.dumps({"toxic_count": toxic_count}),
        )
    except Exception as exc:
        log.warning("safety_metrics_save_failed",
                    trace_id=trace_id, error=str(exc))

    result = SafetyResult(
        trace_id=trace_id,
        agent_role=agent_role,
        run_id=run_id,
        injection_detected=injection_count > 0,
        injection_count=injection_count,
        injection_patterns_hit=all_injection_patterns_hit,
        jailbreak_detected=jailbreak_count > 0,
        jailbreak_count=jailbreak_count,
        toxic_output_count=toxic_count,
        toxic_rate=round(toxic_rate, 4),
        bias_flagged=bias_flagged,
        guardrail_blocks=guardrail_blocks,
        guardrail_rate=round(guardrail_rate, 4),
        total_task_spans=total_task,
    )

    log.info(
        "safety_checks_complete",
        trace_id=trace_id,
        agent_role=agent_role,
        injection_detected=result.injection_detected,
        injection_count=injection_count,
        jailbreak_detected=result.jailbreak_detected,
        jailbreak_count=jailbreak_count,
        toxic_output_count=toxic_count,
        toxic_rate=round(toxic_rate, 4),
        bias_flagged=bias_flagged,
        guardrail_blocks=guardrail_blocks,
        guardrail_rate=round(guardrail_rate, 4),
        total_task_spans=total_task,
    )

    return result
