"""
Example: LangChain Agent with Automatic Adapter Instrumentation
============================================================
Demonstrates using LangChainEvalAdapter as a callback handler.
Works with any LangChain agent - zero changes to agent logic.

Prerequisites:
  pip install langchain langchain-community langchain-openai \
              opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc \
              opentelemetry-instrumentation-anthropic

  OPENAI_API_KEY environment variable must be set (used by the LLM below).
  OTel Collector running at localhost:4317 (or set OTEL_EXPORTER_OTLP_ENDPOINT).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# ---------------------------------------------------------------------------
# Initialise telemetry FIRST — before importing LangChain or the LLM client
# ---------------------------------------------------------------------------
from instrumentation import init_telemetry
from instrumentation.adapters import LangChainEvalAdapter

run_id = init_telemetry(
    service_name="langchain-example",
)
print(f"[telemetry] run_id={run_id}")
print(f"            Search for run_id='{run_id}' in Grafana to see all spans.\n")

# ---------------------------------------------------------------------------
# LangChain agent setup  (wrapped in try/except with install hint)
# ---------------------------------------------------------------------------
try:
    from langchain_openai import ChatOpenAI
    from langchain.agents import AgentExecutor, create_react_agent
    from langchain_community.tools import DuckDuckGoSearchRun
    from langchain_core.prompts import PromptTemplate
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    _LANGCHAIN_AVAILABLE = False


def build_agent() -> "AgentExecutor":
    """
    Build a simple ReAct agent equipped with a DuckDuckGo search tool.
    The LangChainEvalAdapter is attached as a callback so every chain
    invocation and tool call automatically emits canonical OTel spans.
    """
    if not _LANGCHAIN_AVAILABLE:
        raise ImportError(
            "LangChain is not installed.\n"
            "Run: pip install langchain langchain-community langchain-openai"
        )

    # LLM
    llm = ChatOpenAI(
        model="gpt-3.5-turbo",
        temperature=0,
    )

    # Tools
    search_tool = DuckDuckGoSearchRun()
    tools = [search_tool]

    # ReAct prompt template (minimal)
    react_prompt = PromptTemplate.from_template(
        "Answer the following question as best you can.\n"
        "You have access to the following tools:\n\n"
        "{tools}\n\n"
        "Use the following format:\n\n"
        "Question: the input question you must answer\n"
        "Thought: you should always think about what to do\n"
        "Action: the action to take, should be one of [{tool_names}]\n"
        "Action Input: the input to the action\n"
        "Observation: the result of the action\n"
        "... (this Thought/Action/Action Input/Observation can repeat N times)\n"
        "Thought: I now know the final answer\n"
        "Final Answer: the final answer to the original input question\n\n"
        "Begin!\n\n"
        "Question: {input}\n"
        "Thought:{agent_scratchpad}"
    )

    # Build agent
    agent = create_react_agent(llm, tools, react_prompt)

    # ---------------------------------------------------------------------------
    # Attach the aieval adapter as a callback - zero changes to agent code
    # ---------------------------------------------------------------------------
    eval_adapter = LangChainEvalAdapter(
        agent_id="lc-react-agent-1",
        agent_role="researcher",
    )

    executor = AgentExecutor(
        agent=agent,
        tools=tools,
        verbose=True,
        callbacks=[eval_adapter],  # <-- canonical spans emitted automatically
        handle_parsing_errors=True,
        max_iterations=4,
    )

    return executor


def run_agent(question: str) -> str:
    """Run the LangChain ReAct agent with the given question."""
    if not _LANGCHAIN_AVAILABLE:
        print(
            "\n[ERROR] LangChain packages are not installed.\n"
            "Install them with:\n\n"
            "  pip install langchain langchain-community langchain-openai\n"
        )
        sys.exit(1)

    print(f"Question: {question}\n")
    agent_executor = build_agent()

    result = agent_executor.invoke({"input": question})
    answer = result.get("output", str(result))
    return answer


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    question = (
        "What are the three most cited recent breakthroughs in AI safety research?"
    )

    answer = run_agent(question)

    print("\n" + "=" * 60)
    print("AGENT ANSWER")
    print("=" * 60)
    print(answer)
    print("=" * 60)
    print(f"\n[done] run_id={run_id}")
    print(f"       Open Grafana and filter by run_id='{run_id}' to see:")
    print("         - agent.task spans (one per chain invocation)")
    print("         - agent.tool_call spans (one per DuckDuckGo search)")
    print("         - LLM spans (auto-instrumented by OpenLLMetry)")


if __name__ == "__main__":
    main()
