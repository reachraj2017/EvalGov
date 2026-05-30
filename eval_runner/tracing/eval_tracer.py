"""
Eval Self-Tracing

Configures an OTel tracer for the eval runner itself so that every
evaluator invocation becomes a span visible in Jaeger alongside the
agent traces it is scoring.

Span hierarchy:
    eval.pipeline  (one per trace evaluation)
    └── eval.score (one per evaluator that runs)

Both are exported via OTLP HTTP to the same OTel Collector the agents use.
Service name: "eval-runner"
"""

import os
from contextlib import contextmanager

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags

_OTLP_ENDPOINT = os.getenv(
    "OTEL_EXPORTER_OTLP_ENDPOINT",
    "http://aieval-otelcol:4318",
)

_provider: TracerProvider | None = None
_tracer: trace.Tracer | None = None


def setup_eval_tracing() -> None:
    """
    Call once at application startup (lifespan) to initialise the tracer.
    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _provider, _tracer
    if _provider is not None:
        return

    resource = Resource.create({"service.name": "eval-runner"})
    _provider = TracerProvider(resource=resource)

    exporter = OTLPSpanExporter(endpoint=f"{_OTLP_ENDPOINT}/v1/traces")
    _provider.add_span_processor(BatchSpanProcessor(exporter))

    trace.set_tracer_provider(_provider)
    _tracer = trace.get_tracer("eval-runner")


def get_tracer() -> trace.Tracer:
    """Return the configured eval tracer (no-op tracer if setup not called)."""
    if _tracer is None:
        return trace.get_tracer("eval-runner")
    return _tracer


@contextmanager
def eval_pipeline_span(trace_id: str, run_id: str, mode: str):
    """
    Context manager that wraps the entire evaluation of one agent trace.

    The span is linked to the agent trace being scored via its trace_id,
    so Jaeger shows eval spans alongside the original agent spans.
    """
    tracer = get_tracer()

    # Convert hex trace_id to int for OTel span context
    try:
        linked_trace_id_int = int(trace_id.replace("-", ""), 16)
        link_ctx = SpanContext(
            trace_id=linked_trace_id_int,
            span_id=0x0000000000000001,
            is_remote=True,
            trace_flags=TraceFlags(TraceFlags.SAMPLED),
        )
        links = [trace.Link(link_ctx)]
    except (ValueError, TypeError):
        links = []

    with tracer.start_as_current_span(
        "eval.pipeline",
        links=links,
        attributes={
            "eval.trace_id": trace_id,
            "eval.run_id":   run_id,
            "eval.mode":     mode,
        },
    ) as span:
        yield span


@contextmanager
def eval_score_span(evaluator: str, metric: str, eval_type: str, trace_id: str, run_id: str):
    """
    Context manager wrapping a single evaluator call.
    Score and reasoning are set as attributes after the evaluator returns.
    """
    tracer = get_tracer()
    with tracer.start_as_current_span(
        "eval.score",
        attributes={
            "eval.evaluator": evaluator,
            "eval.metric":    metric,
            "eval.eval_type": eval_type,
            "eval.trace_id":  trace_id,
            "eval.run_id":    run_id,
        },
    ) as span:
        yield span
