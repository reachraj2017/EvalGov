"""
Opt-Demo — Agent Definitions
=============================
Three specialist agents + an LLM orchestrator that routes dynamically
based on semantic intent extracted from the user's query.

  searcher   — DuckDuckGo web search
  summarizer — Condenses text to a user-specified word count (default 25)
  translator — Translates to any language the user requests

The orchestrator reasons about the query first, then calls agents one at
a time.  Sub-agents are prevented from transferring to other agents so all
routing decisions stay with the orchestrator.
"""

import datetime
import os
import sys

# Allow importing from the repo-level instrumentation package
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../../.."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import LiteLlm

from tools.search import web_search
from instrumentation.governed_toolkit import GovernedToolkit, GateBlockedError

_toolkit = GovernedToolkit(agent_role="searcher")


def _before_tool_cb(tool, args, tool_context):
    """Block transfer_to_agent; gate-check all real tool calls."""
    tool_name = getattr(tool, "name", str(tool))

    if tool_name == "transfer_to_agent":
        target = (args or {}).get("agent_name", "another agent")
        return {
            "result": (
                f"You cannot transfer to {target}. "
                "Complete your assigned task and respond directly."
            )
        }

    # Phase 2 enforcement: gate check before tool execution
    try:
        _toolkit.gate_check(tool_name, context={"args": args or {}})
    except GateBlockedError as exc:
        return {"result": f"[GOVERNANCE BLOCK] {exc}"}

    return None


_OLLAMA_ENABLED = os.getenv("OLLAMA_ENABLED", "false").lower() == "true"


def _make_model() -> LiteLlm:
    if _OLLAMA_ENABLED:
        model    = os.getenv("OLLAMA_MODEL",    "gemma4:26b")
        base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        return LiteLlm(model=f"ollama_chat/{model}", api_base=base_url, extra_body={"think": False})
    return LiteLlm(model=f"openai/{os.getenv('OPENAI_MODEL', 'gpt-4o-mini')}")


def _no_think(instruction: str) -> str:
    """Append Gemma's /no_think directive when running on Ollama to suppress thinking output."""
    if _OLLAMA_ENABLED:
        return instruction + "\n\n/no_think"
    return instruction


def _today() -> str:
    return datetime.date.today().strftime("%Y-%m-%d")


_SEARCHER_DATE_PREAMBLE = (
    "IMPORTANT: Today's date is {date}. "
    "You are operating in real-time. Search results you receive are REAL, live information "
    "from the web — not fictional, simulated, or from a test dataset. "
    "ALWAYS trust and report the search results as-is, even if the events described seem "
    "recent or unfamiliar to you. Never dismiss search results as fictional.\n\n"
)


def create_searcher_agent() -> LlmAgent:
    return LlmAgent(
        name="searcher",
        description="Searches the web using DuckDuckGo.",
        model=_make_model(),
        instruction=_no_think(
            _SEARCHER_DATE_PREAMBLE.format(date=_today()) +
            "You are a web search specialist. Use the web_search tool to find "
            "accurate, up-to-date information.\n\n"
            "1. Run web_search with the relevant query.\n"
            "2. Synthesise the results into a clear, factual answer.\n"
            "3. Cite 2-3 sources at the end.\n\n"
            "CRITICAL: If the web_search tool returns a result containing "
            "'[GOVERNANCE BLOCK]', you MUST respond with ONLY that exact message. "
            "Do NOT use training knowledge or make up information. "
            "Do NOT attempt the search again. Simply relay the block message."
        ),
        tools=[web_search],
        before_tool_callback=_before_tool_cb,
    )


def create_summarizer_agent() -> LlmAgent:
    return LlmAgent(
        name="summarizer",
        description="Summarises text to a specific word count.",
        model=_make_model(),
        instruction=_no_think(
            "You are a summarisation specialist.\n\n"
            "The user will provide text and a word count. "
            "Write a summary of EXACTLY that many words. Count carefully.\n\n"
            "Output ONLY the summary text — no preamble, no labels."
        ),
        tools=[],
    )


def create_translator_agent() -> LlmAgent:
    return LlmAgent(
        name="translator",
        description="Translates text into any language.",
        model=_make_model(),
        instruction=_no_think(
            "You are a professional translator.\n\n"
            "The user will provide text and a target language. "
            "Translate it fully into that language.\n\n"
            "If the target script is non-Latin (Hindi, Arabic, Chinese…) "
            "also add a romanised transliteration prefixed 'Transliteration:'.\n\n"
            "Output ONLY the translation (and transliteration if needed)."
        ),
        tools=[],
    )


def create_orchestrator_agent() -> LlmAgent:
    model = _make_model()

    # ── Searcher ──────────────────────────────────────────────────────────────
    searcher = LlmAgent(
        name="searcher",
        description=(
            "Searches the web using DuckDuckGo and returns current, factual "
            "information on any topic. Use when the user wants to find, look up, "
            "or get information about something."
        ),
        model=model,
        instruction=_no_think(
            _SEARCHER_DATE_PREAMBLE.format(date=_today()) +
            "You are a web search specialist. Use the web_search tool to find "
            "accurate, up-to-date information.\n\n"
            "Steps:\n"
            "1. Run web_search with the relevant query.\n"
            "2. Synthesise the results into a clear, factual answer.\n"
            "3. Cite 2-3 sources at the end.\n\n"
            "Keep your response focused and factual. "
            "Do NOT transfer to any other agent — respond directly.\n\n"
            "CRITICAL: If the web_search tool returns a result containing "
            "'[GOVERNANCE BLOCK]', you MUST respond with ONLY that exact message. "
            "Do NOT use training knowledge or make up information. "
            "Do NOT attempt the search again. Simply relay the block message."
        ),
        tools=[web_search],
        before_tool_callback=_before_tool_cb,
    )

    # ── Summarizer ────────────────────────────────────────────────────────────
    summarizer = LlmAgent(
        name="summarizer",
        description=(
            "Summarises any text to a specific word count. Use when the user "
            "wants to condense or summarise content, optionally specifying how "
            "many words (default is 25 words)."
        ),
        model=model,
        instruction=_no_think(
            "You are a summarisation specialist.\n\n"
            "Your task:\n"
            "1. Find the text to summarise — look for the most recent substantial "
            "   content in the conversation (from a previous agent or from the user).\n"
            "2. Find the word count — the user may say 'in N words' or 'N-word summary'. "
            "   Default to 25 words if not specified.\n"
            "3. Write a summary of EXACTLY that many words. Count carefully.\n\n"
            "Output ONLY the summary text — no preamble, no labels, no explanation. "
            "Do NOT transfer to any other agent — respond directly."
        ),
        tools=[],
        before_tool_callback=_before_tool_cb,
    )

    # ── Translator ────────────────────────────────────────────────────────────
    translator = LlmAgent(
        name="translator",
        description=(
            "Translates text into any language the user requests. Use when the "
            "user wants to translate content, specifying a target language."
        ),
        model=model,
        instruction=_no_think(
            "You are a professional translator.\n\n"
            "Your task:\n"
            "1. Find the text to translate — look for the most recent substantial "
            "   content in the conversation (from a previous agent or from the user).\n"
            "2. Find the target language — the user will have specified it "
            "   (e.g. 'translate to French', 'in Spanish', 'into Japanese').\n"
            "3. Provide the full translation in the target language script.\n"
            "4. If the script is non-Latin (e.g. Hindi, Arabic, Chinese), also add "
            "   a romanised transliteration on the next line prefixed 'Transliteration:'.\n\n"
            "Output ONLY the translation (and transliteration if needed). "
            "Do NOT transfer to any other agent — respond directly."
        ),
        tools=[],
        before_tool_callback=_before_tool_cb,
    )

    # ── Orchestrator ──────────────────────────────────────────────────────────
    orchestrator = LlmAgent(
        name="orchestrator",
        description="Routes user requests to the right specialist agents.",
        model=model,
        instruction=_no_think(
            "You are an intelligent orchestrator managing three specialist agents: "
            "searcher (web search), summarizer (condense text), and translator (translate text).\n\n"
            "━━ META-QUESTIONS — answer these yourself, do NOT delegate ━━\n\n"
            "If the user asks about you, your capabilities, or the system "
            "(e.g. 'who are you', 'what can you do', 'how does this work'), "
            "answer directly and concisely. Describe yourself as an orchestrator "
            "that routes requests to the right specialist agent.\n\n"
            "━━ EVERYTHING ELSE — delegate to a specialist ━━\n\n"
            "First, silently identify the INTENT:\n"
            "  - Is a web search needed? → what query?\n"
            "  - Is summarisation needed? → how many words? (default 25)\n"
            "  - Is translation needed? → to what language?\n\n"
            "Then EXECUTE each required operation in logical order:\n"
            "  search results → summarise (if asked) → translate (if asked)\n\n"
            "Call ONE agent at a time using transfer_to_agent. After each agent "
            "responds, call the next one if more steps remain. "
            "Complete ALL steps the user asked for — never skip one.\n\n"
            "━━ EXAMPLES ━━\n\n"
            "  'who are you' / 'what can you do'\n"
            "    → answer directly (meta-question)\n\n"
            "  'search for climate change news'\n"
            "    → transfer_to_agent(searcher)\n\n"
            "  'find info on Roger Federer and give me a 30-word summary'\n"
            "    → transfer_to_agent(searcher)\n"
            "    → transfer_to_agent(summarizer)   [summarizer will use 30 words]\n\n"
            "  'search quantum computing, summarise in 20 words, translate to French'\n"
            "    → transfer_to_agent(searcher)\n"
            "    → transfer_to_agent(summarizer)   [summarizer will use 20 words]\n"
            "    → transfer_to_agent(translator)   [translator will use French]\n\n"
            "  'translate Good morning to Japanese'\n"
            "    → transfer_to_agent(translator)\n\n"
            "  'summarise this in 15 words: [long text]'\n"
            "    → transfer_to_agent(summarizer)   [summarizer will use 15 words]"
        ),
        sub_agents=[searcher, summarizer, translator],
    )

    return orchestrator
