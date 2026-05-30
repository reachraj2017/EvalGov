"""
Runner — Opt-Demo
=================
Wraps Google ADK with OTel instrumentation.

Routing strategy:
  - Simple queries (search only / translate only / summarize only) go through
    the orchestrator runner — one LLM decision, reliable.
  - Multi-step pipeline (search + summarize + translate) is driven EXPLICITLY
    in code by calling each sub-agent runner directly.  The orchestrator is
    bypassed for steps 2/3 because GPT-4o-mini consistently routes every step
    to the searcher when given the full conversation context.

Span hierarchy for a pipeline query:
  agent.task  [orchestrator, root]
    agent.task  [searcher]
      agent.tool_call  [web_search]
    agent.task  [summarizer]
    agent.task  [translator]

All spans share run.id so the eval runner groups them correctly.
Provider is kept PRIVATE (not global) so ADK's own internal OTel spans
don't flow through our exporter and create duplicates.
"""

import asyncio
import os
import re
import sys
import time
import uuid

import httpx
import google.genai.types as genai_types
from google.adk.events import Event
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from opentelemetry import trace, context as otel_context

from demo_telemetry import get_tracer, get_run_id, init_demo_telemetry

# ── Axis 3 — SLM feature flag ─────────────────────────────────────────────────
# Set AXIS3_SLM_ENABLED=false to bypass all SLMs and use the gpt-4o-mini path.
_AXIS3_ENABLED = os.getenv("AXIS3_SLM_ENABLED", "true").lower() != "false"

# ── Bootstrap telemetry ───────────────────────────────────────────────────────
init_demo_telemetry()

# ── Circuit breaker check (runner-level, pre-LLM) ────────────────────────────
_GOV_URL  = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
_CB_CACHE: dict[str, tuple[str, float]] = {}   # role → (state, expires_at)
_P2_CACHE: list[tuple[bool, float]]     = []   # [0] = (enabled, expires_at)
_CB_TTL   = 30.0


def _phase2_enabled() -> bool:
    """Return whether Phase 2 enforcement is enabled on the backend (cached 30 s)."""
    if _P2_CACHE and time.monotonic() < _P2_CACHE[0][1]:
        return _P2_CACHE[0][0]
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{_GOV_URL}/enforcement/phase2/status")
            resp.raise_for_status()
            enabled = bool(resp.json().get("phase2_enabled", False))
            if _P2_CACHE:
                _P2_CACHE[0] = (enabled, time.monotonic() + _CB_TTL)
            else:
                _P2_CACHE.append((enabled, time.monotonic() + _CB_TTL))
            return enabled
    except Exception:
        pass
    return False  # fail open — governance service unreachable


def _agent_gate_check(
    agent_role: str,
    user_query: str = "",
    trace_id: str = "",
    run_id: str = "",
) -> tuple[str, str]:
    """Call the governance gate for an agent-level invocation.

    Returns (decision, request_id).
    decision: auto_approve | flag | pause | block
    """
    try:
        with httpx.Client(timeout=3.0) as client:
            ctx: dict = {"agent": agent_role}
            if user_query:
                ctx["query"] = user_query[:500]
            resp = client.post(
                f"{_GOV_URL}/gate/check",
                json={
                    "action_type": "agent_invoke",
                    "agent_role":  agent_role,
                    "context":     ctx,
                    "trace_id":    trace_id,
                    "run_id":      run_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("decision", "auto_approve"), data.get("request_id", "")
    except Exception:
        return "auto_approve", ""  # fail open


_HITL_POLL   = 3.0   # seconds between polls
_HITL_TIMEOUT = float(os.getenv("HITL_TIMEOUT_SECONDS", "300"))


def _wait_for_hitl_agent_approval(request_id: str, agent_role: str) -> str:
    """Poll until operator approves or rejects the agent invocation.

    Returns 'approved', 'rejected', or 'timeout'.
    """
    deadline = time.monotonic() + _HITL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(f"{_GOV_URL}/hitl/{request_id}/status")
                if resp.status_code == 200:
                    status = resp.json().get("status", "pending")
                    if status == "approved":
                        return "approved"
                    if status in ("rejected", "expired"):
                        return status
        except Exception:
            pass
        time.sleep(_HITL_POLL)
    return "timeout"


# ── Axis 3 span helpers ───────────────────────────────────────────────────────

def _emit_slm_spans(
    child_span,
    tracer,
    run_id: str,
    parent_ctx,
    model_id: str,
    params_b: float,
    confidence: float,
    escalated: bool,
    in_tokens: int,
    out_tokens: int,
) -> None:
    """
    Set Axis 3 attributes on the existing child agent.task span and emit a
    manual agent.llm_call span so the eval system tracks model + token usage.
    """
    # Axis 3 custom attributes on the agent.task span
    child_span.set_attribute("ios.axis3.model_used",       model_id)
    child_span.set_attribute("ios.axis3.model_params_b",   params_b)
    child_span.set_attribute("ios.axis3.confidence_score", round(confidence, 4))
    child_span.set_attribute("ios.axis3.escalated",        escalated)

    # Manual llm_call span nested under the agent.task child span (not root)
    # so token rollup in eval/gov attributes to the correct agent, not orchestrator
    child_ctx = trace.set_span_in_context(child_span)
    llm_span  = tracer.start_span("agent.llm_call", context=child_ctx)
    llm_span.set_attribute("gen_ai.request.model",      model_id)
    llm_span.set_attribute("gen_ai.usage.input_tokens",  in_tokens)
    llm_span.set_attribute("gen_ai.usage.output_tokens", out_tokens)
    llm_span.set_attribute("run.id",                     run_id)
    llm_span.end()


# ── Session / runner store ────────────────────────────────────────────────────
_session_service = InMemorySessionService()
_sessions: dict[str, str] = {}        # user_id → session_id for orchestrator path
_runners: dict[str, Runner] = {}       # agent_name → Runner
APP_NAME = "opt-demo"

_SUB_AGENT_NAMES = {"searcher", "summarizer", "translator"}


def _get_runner(agent_name: str) -> Runner:
    """Lazily create and cache a Runner for each agent."""
    if agent_name not in _runners:
        from agents import (
            create_orchestrator_agent,
            create_searcher_agent,
            create_summarizer_agent,
            create_translator_agent,
        )
        factories = {
            "orchestrator": create_orchestrator_agent,
            "searcher":     create_searcher_agent,
            "summarizer":   create_summarizer_agent,
            "translator":   create_translator_agent,
        }
        _runners[agent_name] = Runner(
            agent=factories[agent_name](),
            app_name=APP_NAME,
            session_service=_session_service,
        )
    return _runners[agent_name]


# ── Query classification ──────────────────────────────────────────────────────

def _classify_query(user_input: str) -> dict:
    """
    Classify the user query and return a routing dict.

    Types:
      pipeline          — search + summarize and/or translate
      search            — search / find only
      summarize         — summarize only (uses session history for content)
      translate         — translate only (uses session history for content)
      summarize_translate — summarize + translate, no search (uses session history)
      passthrough       — everything else; fall through to orchestrator
    """
    lower = user_input.lower()

    has_search    = bool(re.search(r'\b(search|find)\b', lower))
    has_summarize = bool(re.search(r'\b(summarize|summarise|summary|condense|shorten)\b', lower))
    has_translate = bool(re.search(r'\btranslat', lower))

    # Word count (default 20)
    wc   = re.search(r'\b(\d+)\s*[\-\s]?words?\b', lower)
    word_count = int(wc.group(1)) if wc else 20

    # Target language — try "translate ... to <lang>" first, then "in <lang>" at end
    lang = re.search(r'\btranslat\w*(?:\s+\w+){0,3}\s+(?:to|into|in)\s+(\w+)', lower)
    if not lang:
        lang = re.search(r'\b(?:to|into|in)\s+(\w+)\s*$', lower)
    language = lang.group(1).capitalize() if lang else None

    # Search topic
    m = re.search(
        r'\b(?:search|find)(?:\s+for)?\s+(.+?)'
        r'(?:\s+(?:and\s+)?then\b|\s*[,;]|\s+and\b|$)',
        lower,
    )
    topic = m.group(1).strip() if m else user_input

    if has_search and (has_summarize or has_translate):
        return {
            "type":       "pipeline",
            "topic":      topic,
            "word_count": word_count if has_summarize else None,
            "language":   language,
        }
    if has_search:
        return {"type": "search", "topic": topic}
    if has_summarize and has_translate:
        return {"type": "summarize_translate", "word_count": word_count, "language": language}
    if has_summarize:
        # Check for explicit inline text: "summarize: <text>" or "summarize this: <text>"
        inline = re.search(r'\b(?:summarize|summarise)\b[\s:]+(.{20,})', user_input, re.IGNORECASE)
        return {"type": "summarize", "word_count": word_count,
                "text": inline.group(1).strip() if inline else None}
    if has_translate:
        # Extract explicit inline text: "translate <text> to/in/into <lang>"
        # Use original user_input (not lower) to preserve casing
        inline = re.search(
            r'\btranslat\w*\s+(.+?)\s+(?:to|into|in)\s+\w+\s*$',
            user_input, re.IGNORECASE,
        )
        explicit_text = inline.group(1).strip() if inline else None
        # Reject if the "text" is just a filler word (≤ 1 word) — means no inline content
        if explicit_text and len(explicit_text.split()) < 2:
            explicit_text = None
        return {"type": "translate", "language": language, "text": explicit_text}
    return {"type": "passthrough"}


# ── Text extraction (protobuf-safe) ──────────────────────────────────────────

def _extract_text(event) -> str:
    content = getattr(event, "content", None)
    if not content:
        return ""
    parts = getattr(content, "parts", None) or []
    for part in parts:
        text = getattr(part, "text", None) or ""
        if text.strip():
            return text
        fr = getattr(part, "function_response", None)
        if fr:
            resp = getattr(fr, "response", None)
            if isinstance(resp, dict):
                for key in ("result", "output", "content"):
                    val = resp.get(key)
                    if val and isinstance(val, str) and val.strip():
                        return val
    return ""


# ── Public API ────────────────────────────────────────────────────────────────

def run_agent(user_input: str, user_id: str = "default", run_id: str | None = None,
              source: str = "production", conversation_id: str | None = None) -> tuple[str, str]:
    """Run the orchestrator and return (response_text, trace_id)."""
    tracer  = get_tracer()
    run_id  = run_id or get_run_id()

    with tracer.start_as_current_span("agent.task") as root_span:
        root_span.set_attribute("agent.id",     "orchestrator")
        root_span.set_attribute("agent.role",   "orchestrator")
        root_span.set_attribute("task.id",      str(uuid.uuid4()))
        root_span.set_attribute("task.input",   user_input[:2000])
        root_span.set_attribute("run.id",       run_id)
        root_span.set_attribute("trace.source", source)
        if conversation_id:
            root_span.set_attribute("conversation.id", conversation_id)

        ctx      = root_span.get_span_context()
        trace_id = format(ctx.trace_id, "032x") if ctx and ctx.trace_id else ""
        root_ctx = otel_context.get_current()

        loop = asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(
                _run_async(user_input, user_id, root_span, root_ctx, tracer, run_id, source, conversation_id)
            )
        except Exception as e:
            root_span.set_attribute("task.status", "failure")
            root_span.record_exception(e)
            raise
        finally:
            loop.close()

        root_span.set_attribute("task.output",  response[:2000])
        root_span.set_attribute("task.status",  "success")

    return response, trace_id


# ── Routing ───────────────────────────────────────────────────────────────────

async def _ensure_session(user_id: str) -> str:
    """Return the session_id for this user, creating one if needed."""
    if user_id not in _sessions:
        s = await _session_service.create_session(app_name=APP_NAME, user_id=user_id)
        _sessions[user_id] = s.id
    return _sessions[user_id]


async def _run_async(user_input, user_id, root_span, root_ctx, tracer, run_id, source="production", conversation_id=None):
    # Always ensure the user's main session exists for context propagation.
    await _ensure_session(user_id)
    session_id = _sessions[user_id]

    intent = _classify_query(user_input)
    itype  = intent["type"]

    # ── Multi-step pipeline: search + summarize and/or translate ──────────────
    if itype == "pipeline":
        return await _run_pipeline(
            intent, user_input, user_id, root_span, root_ctx, tracer, run_id, source, conversation_id
        )

    # ── Search only ───────────────────────────────────────────────────────────
    if itype == "search":
        topic = intent["topic"]
        if _AXIS3_ENABLED:
            result = await _slm_search(
                topic=topic, user_input=user_input, user_id=user_id,
                root_span=root_span, root_ctx=root_ctx, tracer=tracer,
                run_id=run_id, source=source, conversation_id=conversation_id,
                session_id=session_id,
            )
        else:
            result = await _call_agent(
                runner=_get_runner("searcher"), agent_name="searcher",
                user_id=user_id, session_id=session_id,
                message=f"Search for information about: {topic}",
                root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
                open_child_span=True, source=source, conversation_id=conversation_id,
            )
        await _inject_history(user_id, user_input, result)
        return result

    # ── Summarize only ────────────────────────────────────────────────────────
    if itype == "summarize":
        wc            = intent["word_count"]
        explicit_text = intent.get("text")
        if _AXIS3_ENABLED:
            text_to_summarize = explicit_text or await _get_last_assistant_message(user_id)
            if text_to_summarize:
                result = await _slm_summarize(
                    text=text_to_summarize, word_count=wc, user_input=user_input, user_id=user_id,
                    root_span=root_span, root_ctx=root_ctx, tracer=tracer,
                    run_id=run_id, source=source, conversation_id=conversation_id,
                    session_id=session_id,
                )
                await _inject_history(user_id, user_input, result)
                return result
        # fallback: no text available or SLM disabled — let gpt-4o-mini use session context
        summarize_msg = (
            f"Summarize the following text in EXACTLY {wc} words:\n\n{explicit_text}"
            if explicit_text
            else f"Summarize the previous response in EXACTLY {wc} words."
        )
        result = await _call_agent(
            runner=_get_runner("summarizer"), agent_name="summarizer",
            user_id=user_id, session_id=session_id,
            message=summarize_msg,
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, source=source, conversation_id=conversation_id,
        )
        await _inject_history(user_id, user_input, result)
        return result

    # ── Translate only ────────────────────────────────────────────────────────
    if itype == "translate":
        lang          = intent.get("language") or "English"
        explicit_text = intent.get("text")
        if _AXIS3_ENABLED:
            text_to_translate = explicit_text or await _get_last_assistant_message(user_id)
            if text_to_translate:
                result = await _slm_translate(
                    text=text_to_translate, language=lang, user_input=user_input, user_id=user_id,
                    root_span=root_span, root_ctx=root_ctx, tracer=tracer,
                    run_id=run_id, source=source, conversation_id=conversation_id,
                    session_id=session_id,
                )
                await _inject_history(user_id, user_input, result)
                return result
        # fallback: no text available or SLM disabled — let gpt-4o-mini use session context
        translate_msg = (
            f"Translate the following text to {lang}:\n\n{explicit_text}"
            if explicit_text
            else f"Translate the previous response to {lang}."
        )
        result = await _call_agent(
            runner=_get_runner("translator"), agent_name="translator",
            user_id=user_id, session_id=session_id,
            message=translate_msg,
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, source=source, conversation_id=conversation_id,
        )
        await _inject_history(user_id, user_input, result)
        return result

    # ── Summarize + translate, no search (content from session history) ───────
    if itype == "summarize_translate":
        wc   = intent["word_count"]
        lang = intent.get("language") or "English"

        uid_m  = f"_pipe_{uuid.uuid4().hex}"
        sess_m = await _session_service.create_session(app_name=APP_NAME, user_id=uid_m)
        last_content = await _get_last_assistant_message(user_id)
        summarize_msg = (
            f"Summarize the following text in EXACTLY {wc} words:\n\n{last_content}"
            if last_content
            else f"Summarize the previous response in EXACTLY {wc} words."
        )
        summary = await _call_agent(
            runner=_get_runner("summarizer"), agent_name="summarizer",
            user_id=uid_m, session_id=sess_m.id,
            message=summarize_msg,
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, source=source, conversation_id=conversation_id,
        )

        uid_t  = f"_pipe_{uuid.uuid4().hex}"
        sess_t = await _session_service.create_session(app_name=APP_NAME, user_id=uid_t)
        result = await _call_agent(
            runner=_get_runner("translator"), agent_name="translator",
            user_id=uid_t, session_id=sess_t.id,
            message=f"Translate the following text to {lang}:\n\n{summary}",
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, source=source, conversation_id=conversation_id,
        )
        await _inject_history(user_id, user_input, result)
        return result

    # ── Passthrough: genuinely ambiguous — let orchestrator decide ────────────
    return await _call_agent(
        runner=_get_runner("orchestrator"), agent_name="orchestrator",
        user_id=user_id, session_id=session_id,
        message=user_input,
        root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
        open_child_span=False, source=source, conversation_id=conversation_id,
    )


async def _get_last_assistant_message(user_id: str) -> str:
    """Return the most recent assistant message from the user's main session."""
    try:
        session_id = await _ensure_session(user_id)
        session = await _session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if not session:
            return ""
        events = getattr(session, "events", []) or []
        for event in reversed(events):
            if getattr(event, "author", "") not in ("user", ""):
                text = _extract_text(event)
                if text.strip():
                    return text
    except Exception as e:
        print(f"[runner] get_last_assistant_message failed: {e}", file=sys.stderr, flush=True)
    return ""


async def _inject_history(user_id: str, user_query: str, assistant_response: str):
    """
    Append the pipeline exchange to the user's main orchestrator session so
    follow-up single-step queries have conversation context.
    """
    try:
        session_id = await _ensure_session(user_id)
        session = await _session_service.get_session(
            app_name=APP_NAME, user_id=user_id, session_id=session_id
        )
        if not session:
            return
        inv_id = str(uuid.uuid4())
        await _session_service.append_event(
            session=session,
            event=Event(
                invocation_id=inv_id,
                author="user",
                content=genai_types.Content(
                    role="user",
                    parts=[genai_types.Part(text=user_query)],
                ),
            ),
        )
        await _session_service.append_event(
            session=session,
            event=Event(
                invocation_id=inv_id,
                author="orchestrator",
                content=genai_types.Content(
                    role="model",
                    parts=[genai_types.Part(text=assistant_response)],
                ),
            ),
        )
    except Exception as e:
        print(f"[runner] history injection failed: {e}", file=sys.stderr, flush=True)


# ── Axis 3 SLM intercept functions ───────────────────────────────────────────

async def _slm_translate(
    text: str, language: str, user_input: str, user_id: str,
    root_span, root_ctx, tracer, run_id: str, source: str,
    conversation_id: str | None, session_id: str,
) -> str:
    """Try NLLB-200 first; fall back to gpt-4o-mini if confidence is too low."""
    from axis3.translator_slm import translate, THRESHOLD, MODEL_ID, PARAMS_B
    from axis3.confidence import score_translation

    # Gate check before SLM executes — same enforcement as _call_agent path
    if _phase2_enabled():
        span_ctx = root_span.get_span_context() if root_span else None
        _tid = format(span_ctx.trace_id, "032x") if span_ctx and span_ctx.is_valid else ""
        decision, request_id = _agent_gate_check("translator", user_query=text[:500], trace_id=_tid, run_id=run_id)
        if decision == "block":
            return (
                "[GOVERNANCE BLOCK] translator agent blocked by governance gate "
                "(circuit breaker OPEN). Contact your administrator."
            )
        if decision == "pause":
            outcome = _wait_for_hitl_agent_approval(request_id, "translator")
            if outcome != "approved":
                return (
                    f"[GOVERNANCE BLOCK] translator agent invocation requires human "
                    f"approval — {outcome}. Contact your administrator."
                )

    token      = otel_context.attach(root_ctx)
    parent_ctx = trace.set_span_in_context(root_span)
    child_span = tracer.start_span("agent.task", context=parent_ctx)
    child_span.set_attribute("agent.id",     "translator")
    child_span.set_attribute("agent.role",   "translator")
    child_span.set_attribute("task.id",      str(uuid.uuid4()))
    child_span.set_attribute("task.input",   text[:2000])
    child_span.set_attribute("run.id",       run_id)
    child_span.set_attribute("trace.source", source)
    if conversation_id:
        child_span.set_attribute("conversation.id", conversation_id)
    child_token = otel_context.attach(trace.set_span_in_context(child_span))

    escalated = False
    result    = ""
    try:
        translated, in_tok, out_tok = translate(text, language)
        confidence = score_translation(translated, text, language)

        if confidence >= THRESHOLD:
            result = translated
            _emit_slm_spans(child_span, tracer, run_id, parent_ctx,
                            MODEL_ID, PARAMS_B, confidence, False, in_tok, out_tok)
        else:
            escalated = True
            print(f"[axis3] translator confidence {confidence:.2f} < {THRESHOLD} — escalating",
                  file=sys.stderr, flush=True)
            _emit_slm_spans(child_span, tracer, run_id, parent_ctx,
                            MODEL_ID, PARAMS_B, confidence, True, in_tok, out_tok)
    except Exception as e:
        escalated = True
        print(f"[axis3] translator SLM error: {e} — escalating", file=sys.stderr, flush=True)

    finally:
        otel_context.detach(child_token)
        otel_context.detach(token)

    if escalated:
        child_span.end()
        translate_msg = f"Translate the following text to {language}:\n\n{text}"
        result = await _call_agent(
            runner=_get_runner("translator"), agent_name="translator",
            user_id=user_id, session_id=session_id,
            message=translate_msg,
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, source=source, conversation_id=conversation_id,
        )
        return result

    child_span.set_attribute("task.output", result[:2000])
    child_span.set_attribute("task.status", "success")
    child_span.end()
    return result


async def _slm_summarize(
    text: str, word_count: int, user_input: str, user_id: str,
    root_span, root_ctx, tracer, run_id: str, source: str,
    conversation_id: str | None, session_id: str,
) -> str:
    """Try BART-large-cnn first; fall back to gpt-4o-mini if confidence is too low."""
    from axis3.summarizer_slm import summarize, THRESHOLD, MODEL_ID, PARAMS_B
    from axis3.confidence import score_summary

    # Gate check before SLM executes — same enforcement as _call_agent path
    if _phase2_enabled():
        span_ctx = root_span.get_span_context() if root_span else None
        _tid = format(span_ctx.trace_id, "032x") if span_ctx and span_ctx.is_valid else ""
        decision, request_id = _agent_gate_check("summarizer", user_query=text[:500], trace_id=_tid, run_id=run_id)
        if decision == "block":
            return (
                "[GOVERNANCE BLOCK] summarizer agent blocked by governance gate "
                "(circuit breaker OPEN). Contact your administrator."
            )
        if decision == "pause":
            outcome = _wait_for_hitl_agent_approval(request_id, "summarizer")
            if outcome != "approved":
                return (
                    f"[GOVERNANCE BLOCK] summarizer agent invocation requires human "
                    f"approval — {outcome}. Contact your administrator."
                )

    token      = otel_context.attach(root_ctx)
    parent_ctx = trace.set_span_in_context(root_span)
    child_span = tracer.start_span("agent.task", context=parent_ctx)
    child_span.set_attribute("agent.id",     "summarizer")
    child_span.set_attribute("agent.role",   "summarizer")
    child_span.set_attribute("task.id",      str(uuid.uuid4()))
    child_span.set_attribute("task.input",   text[:2000])
    child_span.set_attribute("run.id",       run_id)
    child_span.set_attribute("trace.source", source)
    if conversation_id:
        child_span.set_attribute("conversation.id", conversation_id)
    child_token = otel_context.attach(trace.set_span_in_context(child_span))

    escalated = False
    result    = ""
    try:
        summary, in_tok, out_tok = summarize(text, word_count)
        confidence = score_summary(summary, word_count)

        if confidence >= THRESHOLD:
            result = summary
            _emit_slm_spans(child_span, tracer, run_id, parent_ctx,
                            MODEL_ID, PARAMS_B, confidence, False, in_tok, out_tok)
        else:
            escalated = True
            print(f"[axis3] summarizer confidence {confidence:.2f} < {THRESHOLD} — escalating",
                  file=sys.stderr, flush=True)
            _emit_slm_spans(child_span, tracer, run_id, parent_ctx,
                            MODEL_ID, PARAMS_B, confidence, True, in_tok, out_tok)
    except Exception as e:
        escalated = True
        print(f"[axis3] summarizer SLM error: {e} — escalating", file=sys.stderr, flush=True)

    finally:
        otel_context.detach(child_token)
        otel_context.detach(token)

    if escalated:
        child_span.end()
        summarize_msg = f"Summarize the following text in EXACTLY {word_count} words:\n\n{text}"
        result = await _call_agent(
            runner=_get_runner("summarizer"), agent_name="summarizer",
            user_id=user_id, session_id=session_id,
            message=summarize_msg,
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, source=source, conversation_id=conversation_id,
        )
        return result

    child_span.set_attribute("task.output", result[:2000])
    child_span.set_attribute("task.status", "success")
    child_span.end()
    return result


async def _slm_search(
    topic: str, user_input: str, user_id: str,
    root_span, root_ctx, tracer, run_id: str, source: str,
    conversation_id: str | None, session_id: str,
) -> str:
    """Run web_search in Python, synthesize with Qwen3-0.6B; fall back to gpt-4o-mini."""
    from axis3.search_slm import synthesize, THRESHOLD, MODEL_ID, PARAMS_B
    from axis3.confidence import score_search_synthesis
    from tools.search import web_search

    # Gate check before SLM executes — same enforcement as _call_agent path
    if _phase2_enabled():
        span_ctx = root_span.get_span_context() if root_span else None
        _tid = format(span_ctx.trace_id, "032x") if span_ctx and span_ctx.is_valid else ""
        decision, request_id = _agent_gate_check("searcher", user_query=topic, trace_id=_tid, run_id=run_id)
        if decision == "block":
            return (
                "[GOVERNANCE BLOCK] searcher agent blocked by governance gate "
                "(circuit breaker OPEN). Contact your administrator."
            )
        if decision == "pause":
            outcome = _wait_for_hitl_agent_approval(request_id, "searcher")
            if outcome != "approved":
                return (
                    f"[GOVERNANCE BLOCK] searcher agent invocation requires human "
                    f"approval — {outcome}. Contact your administrator."
                )

    token      = otel_context.attach(root_ctx)
    parent_ctx = trace.set_span_in_context(root_span)
    child_span = tracer.start_span("agent.task", context=parent_ctx)
    child_span.set_attribute("agent.id",     "searcher")
    child_span.set_attribute("agent.role",   "searcher")
    child_span.set_attribute("task.id",      str(uuid.uuid4()))
    child_span.set_attribute("task.input",   topic[:2000])
    child_span.set_attribute("run.id",       run_id)
    child_span.set_attribute("trace.source", source)
    if conversation_id:
        child_span.set_attribute("conversation.id", conversation_id)
    child_token = otel_context.attach(trace.set_span_in_context(child_span))

    escalated = False
    result    = ""
    try:
        # Step 1: Python tool call (unchanged)
        raw_results = web_search(topic)

        # Emit tool span
        tool_span = tracer.start_span("agent.tool_call", context=parent_ctx)
        tool_span.set_attribute("agent.id",    "searcher")
        tool_span.set_attribute("tool.name",   "web_search")
        tool_span.set_attribute("tool.input",  topic[:500])
        tool_span.set_attribute("tool.output", raw_results[:500])
        tool_span.set_attribute("run.id",      run_id)
        tool_span.set_attribute("task.status", "success")
        tool_span.end()

        # Step 2: SLM synthesis
        answer, in_tok, out_tok = synthesize(topic, raw_results)
        confidence = score_search_synthesis(answer, raw_results)

        if confidence >= THRESHOLD:
            result = answer
            _emit_slm_spans(child_span, tracer, run_id, parent_ctx,
                            MODEL_ID, PARAMS_B, confidence, False, in_tok, out_tok)
        else:
            escalated = True
            print(f"[axis3] searcher confidence {confidence:.2f} < {THRESHOLD} — escalating",
                  file=sys.stderr, flush=True)
            _emit_slm_spans(child_span, tracer, run_id, parent_ctx,
                            MODEL_ID, PARAMS_B, confidence, True, in_tok, out_tok)
    except Exception as e:
        escalated = True
        print(f"[axis3] searcher SLM error: {e} — escalating", file=sys.stderr, flush=True)

    finally:
        otel_context.detach(child_token)
        otel_context.detach(token)

    if escalated:
        child_span.end()
        result = await _call_agent(
            runner=_get_runner("searcher"), agent_name="searcher",
            user_id=user_id, session_id=session_id,
            message=f"Search for information about: {topic}",
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, source=source, conversation_id=conversation_id,
        )
        return result

    child_span.set_attribute("task.output", result[:2000])
    child_span.set_attribute("task.status", "success")
    child_span.end()
    return result


# ── Pipeline (multi-step) ─────────────────────────────────────────────────────

async def _run_pipeline(intent, user_input, user_id, root_span, root_ctx, tracer, run_id, source="production", conversation_id=None):
    """
    Drive search → [summarize] → [translate] by calling each sub-agent
    runner directly with explicit content.  After completion, inject the
    exchange into the user's main orchestrator session for multi-turn context.

    When _AXIS3_ENABLED, each step routes through the SLM intercept first
    (Qwen3 → BART → NLLB-200) with per-step confidence gating and escalation.
    The output of each step is passed explicitly as the next step's input —
    no session context is shared between steps.
    """
    topic      = intent["topic"]
    word_count = intent["word_count"]
    language   = intent["language"]
    result     = ""

    # ── Step 1: search ────────────────────────────────────────────────────────
    uid_s  = f"_pipe_{uuid.uuid4().hex}"
    sess_s = await _session_service.create_session(app_name=APP_NAME, user_id=uid_s)
    if _AXIS3_ENABLED:
        result = await _slm_search(
            topic, user_input, uid_s,
            root_span, root_ctx, tracer, run_id, source, conversation_id, sess_s.id,
        )
    else:
        result = await _call_agent(
            runner=_get_runner("searcher"),
            agent_name="searcher",
            user_id=uid_s, session_id=sess_s.id,
            message=f"Search for information about: {topic}",
            root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
            open_child_span=True, source=source, conversation_id=conversation_id,
        )

    # ── Step 2: summarize (receives step 1 output as explicit text) ───────────
    if word_count is not None:
        uid_m  = f"_pipe_{uuid.uuid4().hex}"
        sess_m = await _session_service.create_session(app_name=APP_NAME, user_id=uid_m)
        if _AXIS3_ENABLED:
            result = await _slm_summarize(
                result, word_count, user_input, uid_m,
                root_span, root_ctx, tracer, run_id, source, conversation_id, sess_m.id,
            )
        else:
            result = await _call_agent(
                runner=_get_runner("summarizer"),
                agent_name="summarizer",
                user_id=uid_m, session_id=sess_m.id,
                message=f"Summarize the following text in EXACTLY {word_count} words:\n\n{result}",
                root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
                open_child_span=True, source=source, conversation_id=conversation_id,
            )

    # ── Step 3: translate (receives step 2 output as explicit text) ───────────
    if language is not None:
        uid_t  = f"_pipe_{uuid.uuid4().hex}"
        sess_t = await _session_service.create_session(app_name=APP_NAME, user_id=uid_t)
        if _AXIS3_ENABLED:
            result = await _slm_translate(
                result, language, user_input, uid_t,
                root_span, root_ctx, tracer, run_id, source, conversation_id, sess_t.id,
            )
        else:
            result = await _call_agent(
                runner=_get_runner("translator"),
                agent_name="translator",
                user_id=uid_t, session_id=sess_t.id,
                message=f"Translate the following text to {language}:\n\n{result}",
                root_span=root_span, root_ctx=root_ctx, tracer=tracer, run_id=run_id,
                open_child_span=True, source=source, conversation_id=conversation_id,
            )

    result = result or "(No response)"

    # Store this exchange in the user's main session so follow-up queries
    # (e.g. "what team does he play for?") have context.
    await _inject_history(user_id, user_input, result)

    return result


# ── Core streaming helper ─────────────────────────────────────────────────────

async def _call_agent(
    runner,
    agent_name: str,
    user_id: str,
    session_id: str,
    message: str,
    root_span,
    root_ctx,
    tracer,
    run_id: str,
    open_child_span: bool,
    source: str = "production",
    conversation_id: str | None = None,
) -> str:
    """
    Send one message to a runner and return its final response text.
    If open_child_span=True, wraps the call in an agent.task child span.
    Re-attaches root OTel context before each call so all spans land in the
    same trace regardless of ADK's internal context mutations.
    """
    # Runner-level enforcement: full gate check before the LLM runs.
    if _phase2_enabled():
        span_ctx = root_span.get_span_context() if root_span else None
        _tid = format(span_ctx.trace_id, "032x") if span_ctx and span_ctx.is_valid else ""
        decision, request_id = _agent_gate_check(agent_name, user_query=message, trace_id=_tid, run_id=run_id)
        if decision == "block":
            return (
                f"[GOVERNANCE BLOCK] {agent_name} agent blocked by governance gate "
                f"(circuit breaker OPEN). Contact your administrator."
            )
        if decision == "pause":
            outcome = _wait_for_hitl_agent_approval(request_id, agent_name)
            if outcome != "approved":
                return (
                    f"[GOVERNANCE BLOCK] {agent_name} agent invocation requires human "
                    f"approval — {outcome}. Contact your administrator."
                )

    # Re-attach root context — ensures child spans stay in the same trace
    # even when called sequentially in the same event loop.
    token      = otel_context.attach(root_ctx)
    parent_ctx = trace.set_span_in_context(root_span)

    child_span = None
    child_token = None
    if open_child_span:
        child_span = tracer.start_span("agent.task", context=parent_ctx)
        child_span.set_attribute("agent.id",     agent_name)
        child_span.set_attribute("agent.role",   agent_name)
        child_span.set_attribute("task.id",      str(uuid.uuid4()))
        child_span.set_attribute("task.input",   message[:2000])
        child_span.set_attribute("run.id",       run_id)
        child_span.set_attribute("trace.source", source)
        if conversation_id:
            child_span.set_attribute("conversation.id", conversation_id)
        # Make child_span the current span so OpenAI LLM calls (via
        # auto-instrumentation) are nested inside this agent span.
        child_token = otel_context.attach(trace.set_span_in_context(child_span))
        parent_ctx = trace.set_span_in_context(child_span)

    content = genai_types.Content(role="user", parts=[genai_types.Part(text=message)])
    response_parts: list[str] = []
    output_text = ""

    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=content,
        ):
            if agent_name == "searcher":
                _maybe_emit_tool_span(event, tracer, run_id, parent_ctx)

            if event.is_final_response():
                text = _extract_text(event)
                if text:
                    response_parts.append(text)

        output_text = "\n".join(response_parts) if response_parts else ""

    except Exception as e:
        if child_span:
            child_span.set_attribute("task.status", "failure")
            child_span.record_exception(e)
        raise
    finally:
        if child_token is not None:
            otel_context.detach(child_token)
        otel_context.detach(token)

    if child_span:
        child_span.set_attribute("task.output", output_text[:2000])
        child_span.set_attribute("task.status", "success")
        child_span.end()

    return output_text


def _maybe_emit_tool_span(event, tracer, run_id: str, parent_ctx) -> None:
    try:
        parts = getattr(getattr(event, "content", None), "parts", None) or []
        for part in parts:
            fc = getattr(part, "function_call", None)
            if fc and getattr(fc, "name", "") == "web_search":
                args  = getattr(fc, "args", {}) or {}
                span  = tracer.start_span("agent.tool_call", context=parent_ctx)
                span.set_attribute("agent.id",   "searcher")
                span.set_attribute("tool.name",  "web_search")
                span.set_attribute("tool.input", (args.get("query", "") or "")[:500])
                span.set_attribute("run.id",     run_id)
                span.set_attribute("task.status","success")
                span.end()
                return
    except Exception as e:
        print(f"[runner] tool_span error: {e}", file=sys.stderr, flush=True)


def reset_session(user_id: str = "default"):
    _sessions.pop(user_id, None)
