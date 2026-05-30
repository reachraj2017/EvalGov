"""
Instrumentation setup for Opt-Demo.
Initialises OpenTelemetry + OpenAI auto-instrumentation.
Call init_demo_telemetry() once at process start.
"""

import os
import sys
from uuid import uuid4

_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _repo_root not in sys.path:
    sys.path.insert(0, _repo_root)

from opentelemetry import trace
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider, ReadableSpan
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.resources import Resource

_run_id: str = ""
_tracer: trace.Tracer | None = None


class _FilterProcessor(SpanProcessor):
    """
    Wraps a delegate processor and only forwards spans that are either:
      1. Our custom spans — have a 'run.id' attribute (set in runner.py), OR
      2. OpenAI LLM call spans — instrumentation scope contains 'openai'

    This drops ADK's own internal agent-invocation spans (they have no run.id
    and no openai scope) which would otherwise duplicate our custom spans.
    """

    def __init__(self, delegate: SpanProcessor):
        self._d = delegate

    def _keep(self, span: ReadableSpan) -> bool:
        attrs = span.attributes or {}
        if attrs.get("run.id"):
            return True
        scope_name = ""
        try:
            scope_name = span.instrumentation_scope.name or ""
        except Exception:
            pass
        if "openai" in scope_name.lower():
            return True
        return False

    def on_start(self, span, parent_context=None):
        self._d.on_start(span, parent_context)

    def on_end(self, span: ReadableSpan):
        if self._keep(span):
            self._d.on_end(span)

    def shutdown(self):
        self._d.shutdown()

    def force_flush(self, timeout_millis: int = 30_000) -> bool:
        return self._d.force_flush(timeout_millis)


def init_demo_telemetry(run_id: str | None = None) -> str:
    """
    Initialise OTel + OpenAI auto-instrumentation.
    Returns the run_id for this session.  Safe to call multiple times.
    """
    global _run_id, _tracer

    if _tracer is not None:
        return _run_id

    _run_id = run_id or str(uuid4())
    endpoint = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")

    resource = Resource.create({
        "service.name":    "opt-demo",
        "service.version": "1.0.0",
        "run.id":          _run_id,
    })

    provider = TracerProvider(resource=resource)

    try:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
        # Export all spans — ADK's intermediate spans (call_llm, generate_content)
        # must be in ClickHouse to form the parent chain from openai.chat back to
        # each agent.task span, enabling per-agent token attribution.
        provider.add_span_processor(BatchSpanProcessor(exporter))
        print(f"[telemetry] Exporting to OTel Collector at {endpoint}")
    except Exception as e:
        print(f"[telemetry] OTel Collector unavailable ({e}) — traces not exported")

    # Set as global provider so OpenAI auto-instrumentation (which uses
    # trace.get_tracer()) picks it up and LLM call spans are exported.
    trace.set_tracer_provider(provider)
    _tracer = provider.get_tracer("opt-demo")

    try:
        from opentelemetry.instrumentation.openai import OpenAIInstrumentor
        OpenAIInstrumentor().instrument()
        print("[telemetry] OpenAI auto-instrumentation active")
    except Exception as e:
        print(f"[telemetry] OpenAI instrumentation skipped: {e}")

    print(f"[telemetry] run_id = {_run_id}")
    return _run_id


def get_tracer() -> trace.Tracer:
    global _tracer
    if _tracer is None:
        init_demo_telemetry()
    return _tracer


def get_run_id() -> str:
    return _run_id
