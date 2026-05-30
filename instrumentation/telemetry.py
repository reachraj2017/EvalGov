"""
OpenTelemetry + OpenLLMetry initialization.
Call init_telemetry() once at process startup before any agent code runs.
"""

import os
from uuid import uuid4

from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource

# Module-level run_id, set during init_telemetry()
_run_id: str = ""

_DEFAULT_OTLP_ENDPOINT = "localhost:4317"


def init_telemetry(
    service_name: str,
    run_id: str = None,
    otlp_endpoint: str = None,
) -> str:
    """
    Initialize OpenTelemetry tracing with OTLP gRPC export and OpenLLMetry auto-instrumentation.

    Args:
        service_name: Logical name for this service/agent system (used as OTel resource attribute).
        run_id: Unique identifier for this evaluation run. Auto-generated (UUID4) if not provided.
        otlp_endpoint: gRPC endpoint for the OTel Collector.
                       Falls back to OTEL_EXPORTER_OTLP_ENDPOINT env var, then "localhost:4317".

    Returns:
        The run_id being used for this run (useful when auto-generated).
    """
    global _run_id

    # Resolve run_id
    _run_id = run_id or str(uuid4())

    # Resolve OTLP endpoint: parameter > env var > default
    endpoint = (
        otlp_endpoint
        or os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", _DEFAULT_OTLP_ENDPOINT)
    )

    # Build resource with service metadata
    resource = Resource.create(
        {
            "service.name": service_name,
            "run.id": _run_id,
        }
    )

    # Create OTLP gRPC exporter
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)

    # Create TracerProvider with BatchSpanProcessor
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(exporter))

    # Set as global TracerProvider
    trace.set_tracer_provider(provider)

    # --- Auto-instrumentation via OpenLLMetry ---

    # Instrument Anthropic SDK (required)
    from opentelemetry.instrumentation.anthropic import AnthropicInstrumentor
    AnthropicInstrumentor().instrument()

    # Instrument OpenAI SDK (optional - may not be installed)
    try:
        from opentelemetry.instrumentation.openai import OpenAIInstrumentor
        OpenAIInstrumentor().instrument()
    except ImportError:
        pass  # OpenAI instrumentation not available; continuing without it

    return _run_id


def get_tracer() -> trace.Tracer:
    """Return the shared tracer for aieval instrumentation spans."""
    return trace.get_tracer("aieval.instrumentation")


def get_run_id() -> str:
    """Return the run_id set during init_telemetry()."""
    return _run_id
