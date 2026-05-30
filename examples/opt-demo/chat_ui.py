"""
Opt-Demo — Chat UI
==================
Streamlit chat interface for the dynamic multi-agent system.

Agents:
  🔍 Searcher   — DuckDuckGo web search
  📝 Summarizer — Condense to any word count (default 25)
  🌐 Translator — Translate to any language
  🤖 Orchestrator — Automatic routing based on your intent

Run:
    streamlit run chat_ui.py
"""

import os
import sys
import time
import uuid
from datetime import datetime

import streamlit as st
from dotenv import load_dotenv

_here = os.path.dirname(os.path.abspath(__file__))
for p in [_here, os.path.abspath(os.path.join(_here, "../.."))]:
    if p not in sys.path:
        sys.path.insert(0, p)

load_dotenv(os.path.join(_here, ".env"))

# ── Page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="Opt-Demo",
    page_icon="🤖",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ── Session state ─────────────────────────────────────────────────────────────
if "messages" not in st.session_state:
    st.session_state.messages = []
if "user_id" not in st.session_state:
    st.session_state.user_id = str(uuid.uuid4())[:8]
if "conversation_id" not in st.session_state:
    st.session_state.conversation_id = str(uuid.uuid4())

# ── Sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.title("🤖 Opt-Demo")
    st.caption("Google ADK · DuckDuckGo · Dynamic routing")

    st.divider()

    st.subheader("Agents")
    st.markdown(
        """
        | Agent | Capability |
        |-------|-----------|
        | 🔍 Searcher | Web search via DuckDuckGo |
        | 📝 Summarizer | Condense to any word count |
        | 🌐 Translator | Translate to any language |
        | 🤖 Orchestrator | Routes based on your intent |
        """
    )

    st.divider()

    st.subheader("Session")
    st.code(f"User: {st.session_state.user_id}", language=None)

    col1, col2 = st.columns(2)
    with col1:
        if st.button("🔄 New Session", use_container_width=True):
            st.session_state.messages = []
            st.session_state.user_id = str(uuid.uuid4())[:8]
            st.session_state.conversation_id = str(uuid.uuid4())
            try:
                from runner import reset_session
                reset_session(st.session_state.user_id)
            except Exception:
                pass
            st.rerun()
    with col2:
        if st.button("🗑 Clear Chat", use_container_width=True):
            st.session_state.messages = []
            st.rerun()

    st.divider()

    st.subheader("💡 Example queries")
    examples = [
        "Search for latest AI news",
        "Find info on climate change and summarize in 30 words",
        "Search quantum computing, summarize in 20 words, translate to Hindi",
        "Translate 'Good morning, how are you?' to Japanese",
        "Summarize in 15 words: The Amazon rainforest produces 20% of the world's oxygen and is home to 10% of all species.",
        "Find news about SpaceX and give me a 25-word French summary",
    ]
    for ex in examples:
        st.caption(f"• {ex}")

# ── Main area ─────────────────────────────────────────────────────────────────
st.title("Opt-Demo Chat")
st.caption(
    "The orchestrator analyses your intent and routes to the right agents automatically. "
    "Ask it to search, summarise, translate — or any combination."
)

st.divider()

# Chat history
for msg in st.session_state.messages:
    avatar = "👤" if msg["role"] == "user" else "🤖"
    with st.chat_message(msg["role"], avatar=avatar):
        st.markdown(msg["content"])
        if msg["role"] == "assistant":
            jaeger_base = os.getenv("PUBLIC_JAEGER_URL", "http://localhost:16686")
            tid = msg.get("trace_id", "")
            if tid:
                st.caption(f"⏱ {msg.get('latency_ms', '?')} ms · [🔍 View trace]({jaeger_base}/trace/{tid})")
            else:
                st.caption(f"⏱ {msg.get('latency_ms', '?')} ms")

# Input
if prompt := st.chat_input("Ask the orchestrator anything…"):

    if not os.getenv("OPENAI_API_KEY"):
        st.error("OPENAI_API_KEY not set — add it to your .env file.")
        st.stop()

    st.session_state.messages.append({"role": "user", "content": prompt})
    with st.chat_message("user", avatar="👤"):
        st.markdown(prompt)

    with st.chat_message("assistant", avatar="🤖"):
        with st.spinner("Thinking…"):
            t0 = time.time()
            trace_id = ""
            try:
                from runner import run_agent
                response, trace_id = run_agent(
                    user_input=prompt,
                    user_id=st.session_state.user_id,
                    source="production",
                    conversation_id=st.session_state.conversation_id,
                )
                latency_ms = int((time.time() - t0) * 1000)
            except Exception as e:
                response = f"❌ Error: {e}"
                latency_ms = int((time.time() - t0) * 1000)

        st.markdown(response)
        jaeger_base = os.getenv("PUBLIC_JAEGER_URL", "http://localhost:16686")
        footer = f"⏱ {latency_ms} ms"
        if trace_id:
            footer += f" · [🔍 View trace]({jaeger_base}/trace/{trace_id})"
        st.caption(footer)

    st.session_state.messages.append({
        "role":       "assistant",
        "content":    response,
        "latency_ms": latency_ms,
        "trace_id":   trace_id,
    })
