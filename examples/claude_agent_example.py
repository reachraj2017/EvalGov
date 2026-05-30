"""
Example: Claude Multi-Agent System with Full Instrumentation
============================================================
Demonstrates a two-agent pipeline:
  1. ResearchAgent: searches and summarizes information
  2. WriterAgent: takes research summary and writes a blog post

Shows:
  - init_telemetry() setup
  - ClaudeEvalAdapter usage
  - agent.handoff span for orchestration
  - Offline benchmark run tagging with run_id

Prerequisites:
  - ANTHROPIC_API_KEY environment variable must be set
  - pip install anthropic opentelemetry-sdk opentelemetry-exporter-otlp-proto-grpc \
               opentelemetry-instrumentation-anthropic
  - OTel Collector running at localhost:4317 (or set OTEL_EXPORTER_OTLP_ENDPOINT)
"""

import os
import sys

# ---------------------------------------------------------------------------
# Telemetry must be initialised before importing anthropic or any agent code
# ---------------------------------------------------------------------------
# Allow an optional run_id to be supplied as the first CLI argument so that
# offline benchmark runs can be correlated across the evaluation platform.
_cli_run_id = sys.argv[1] if len(sys.argv) > 1 else None

import sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from instrumentation import init_telemetry
from instrumentation.adapters import ClaudeEvalAdapter
from instrumentation.spans import agent_handoff

run_id = init_telemetry(
    service_name="claude-example",
    run_id=_cli_run_id,
)
print(f"[telemetry] run_id={run_id}  (use this to query spans in Grafana)")

# ---------------------------------------------------------------------------
# Anthropic client  –  ANTHROPIC_API_KEY must be set in the environment
# ---------------------------------------------------------------------------
import anthropic  # noqa: E402  (import after telemetry init so OpenLLMetry hooks it)

_client = anthropic.Anthropic()
_MODEL = "claude-3-haiku-20240307"


# ---------------------------------------------------------------------------
# Agents
# ---------------------------------------------------------------------------


class ResearchAgent:
    """
    Researches a topic by asking Claude for a structured summary.
    Every LLM call is auto-instrumented by OpenLLMetry; the outer task
    boundary is captured by ClaudeEvalAdapter.
    """

    def __init__(self):
        self._adapter = ClaudeEvalAdapter(
            agent_id="research-agent-1",
            role="researcher",
        )

    def research(self, topic: str) -> str:
        """
        Run a research task on *topic* and return a concise summary string.
        """
        print(f"\n[ResearchAgent] Researching: {topic}")

        with self._adapter.task(
            task_input=f"Research the following topic and produce a structured summary: {topic}"
        ) as task:
            response = _client.messages.create(
                model=_MODEL,
                max_tokens=512,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"You are a research assistant. "
                            f"Research the following topic and produce a concise, "
                            f"well-structured summary in 3-5 bullet points:\n\n{topic}"
                        ),
                    }
                ],
            )
            summary = response.content[0].text
            task.set_output(summary)
            task.set_status("success")

        print(f"[ResearchAgent] Summary produced ({len(summary)} chars)")
        return summary


class WriterAgent:
    """
    Takes a research summary and writes a short blog post section.
    """

    def __init__(self):
        self._adapter = ClaudeEvalAdapter(
            agent_id="writer-agent-1",
            role="writer",
        )

    def write(self, topic: str, research_summary: str) -> str:
        """
        Generate a blog post section from *research_summary* and return it.
        """
        print(f"\n[WriterAgent] Writing blog post section for: {topic}")

        with self._adapter.task(
            task_input=(
                f"Write a blog post section about '{topic}' "
                f"based on the following research summary."
            )
        ) as task:
            response = _client.messages.create(
                model=_MODEL,
                max_tokens=512,
                messages=[
                    {
                        "role": "user",
                        "content": (
                            f"You are a professional blog writer. "
                            f"Using the research summary below, write an engaging "
                            f"200-word blog post section about '{topic}'.\n\n"
                            f"Research summary:\n{research_summary}"
                        ),
                    }
                ],
            )
            blog_section = response.content[0].text
            task.set_output(blog_section)
            task.set_status("success")

        print(f"[WriterAgent] Blog section produced ({len(blog_section)} chars)")
        return blog_section


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


class Orchestrator:
    """
    Coordinates ResearchAgent → WriterAgent pipeline.
    Emits an agent.handoff span when research output is passed to the writer.
    """

    def __init__(self):
        self._researcher = ResearchAgent()
        self._writer = WriterAgent()

    def run(self, topic: str) -> str:
        print(f"\n[Orchestrator] Starting pipeline for topic: '{topic}'")

        # Step 1: Research
        research_result = self._researcher.research(topic)

        # Step 2: Handoff - emit span capturing the context transfer
        print("\n[Orchestrator] Handing off research to WriterAgent...")
        with agent_handoff(
            from_agent_id="research-agent-1",
            to_agent_id="writer-agent-1",
            reason="research_complete",
            context_payload={
                "topic": topic,
                "summary_length": len(research_result),
                "summary_preview": research_result[:120],
            },
        ):
            # Step 3: Write (runs inside the handoff span so it nests correctly)
            blog_section = self._writer.write(topic, research_result)

        return blog_section


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    topic = "The impact of large language models on scientific research"

    orchestrator = Orchestrator()
    final_output = orchestrator.run(topic)

    print("\n" + "=" * 60)
    print("FINAL BLOG SECTION")
    print("=" * 60)
    print(final_output)
    print("=" * 60)
    print(f"\n[done] run_id={run_id}")
    print(f"       Search for run_id='{run_id}' in Grafana to see all spans.")


if __name__ == "__main__":
    main()
