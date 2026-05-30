"""
Eval Runner - Main FastAPI Application

Receives OTLP trace data, stores spans, triggers evaluation pipeline
on completed agent.task spans.
"""

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional

import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db.repository import Repository
from ingestion.span_receiver import SpanReceiver
from ingestion.trace_assembler import TraceAssembler
from pipeline.eval_pipeline import EvalPipeline
from pipeline.conversation_eval import ConversationEvalPipeline
from pipeline.trigger import EvalTrigger
from regression.comparator import RegressionComparator
from tracing.eval_tracer import setup_eval_tracing

# ------------------------------------------------------------------
# Conversation tracker: conversation_id → {last_seen, run_id}
# Conversations idle for > CONV_IDLE_SECONDS trigger multi-turn eval.
# ------------------------------------------------------------------
_conversation_tracker: dict[str, dict] = {}
CONV_IDLE_SECONDS = 60

# ------------------------------------------------------------------
# Eval dedup: trace_id → last_eval_triggered_at (epoch seconds)
# Prevents re-running the eval pipeline when the same trace_id
# arrives in multiple OTLP batches (e.g. one batch per sub-agent).
# ------------------------------------------------------------------
_eval_triggered: dict[str, float] = {}
EVAL_DEDUP_SECONDS = 30   # suppress re-triggers within this window

# In-progress lock: trace_ids currently being evaluated.
# Prevents concurrent online + offline pipeline runs for the same trace,
# which causes duplicate prompt_eval rows due to ClickHouse TOCTOU races.
_pipeline_in_progress: set[str] = set()

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger(__name__)

# ------------------------------------------------------------------
# Application-level singletons (initialised at startup)
# ------------------------------------------------------------------
_repo: Optional[Repository] = None
_span_receiver: Optional[SpanReceiver] = None
_trace_assembler: Optional[TraceAssembler] = None
_pipeline: Optional[EvalPipeline] = None
_conv_pipeline: Optional[ConversationEvalPipeline] = None
_trigger: Optional[EvalTrigger] = None
_comparator: Optional[RegressionComparator] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise shared singletons on startup."""
    global _repo, _span_receiver, _trace_assembler, _pipeline, _conv_pipeline, _trigger, _comparator

    config_path = os.getenv(
        "EVALUATOR_CONFIG_PATH",
        os.path.join(os.path.dirname(__file__), "config", "evaluator_config.yaml"),
    )

    setup_eval_tracing()

    _repo = Repository()
    _span_receiver = SpanReceiver()
    _trace_assembler = TraceAssembler(repository=_repo)
    _pipeline = EvalPipeline(
        repository=_repo,
        trace_assembler=_trace_assembler,
        evaluator_config_path=config_path,
    )
    _conv_pipeline = ConversationEvalPipeline(repository=_repo)
    _trigger = EvalTrigger()
    _comparator = RegressionComparator()

    # Start background task that fires conversation eval on idle conversations
    asyncio.create_task(_conversation_eval_loop())

    log.info("eval_runner_started")
    yield
    log.info("eval_runner_stopped")


app = FastAPI(
    title="Eval Runner",
    description="Multi-agent AI evaluation platform – OTLP ingestion and scoring service",
    version="1.0.0",
    lifespan=lifespan,
)


# ------------------------------------------------------------------
# Dependency helpers
# ------------------------------------------------------------------

def get_repo() -> Repository:
    if _repo is None:
        raise RuntimeError("Repository not initialised")
    return _repo


def get_pipeline() -> EvalPipeline:
    if _pipeline is None:
        raise RuntimeError("EvalPipeline not initialised")
    return _pipeline


def get_trigger() -> EvalTrigger:
    if _trigger is None:
        raise RuntimeError("EvalTrigger not initialised")
    return _trigger


def get_comparator() -> RegressionComparator:
    if _comparator is None:
        raise RuntimeError("RegressionComparator not initialised")
    return _comparator


# ------------------------------------------------------------------
# Pydantic request/response models
# ------------------------------------------------------------------

class CreateRunRequest(BaseModel):
    name: str
    suite: str = ""
    agent_version: str = ""
    metadata: dict = {}


class CreateRunResponse(BaseModel):
    run_id: str
    name: str


class EvaluateRunRequest(BaseModel):
    mode: str = "offline"   # "offline" or "online"


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Liveness probe."""
    return {"status": "ok", "service": "eval-runner"}


# ---- OTLP ingestion ------------------------------------------------

@app.post("/v1/traces", status_code=200)
async def receive_traces(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """
    Receive OTLP trace data (JSON or protobuf).

    Returns 200 immediately so the OTel Collector never times out waiting.
    All span parsing, persistence, and eval triggering happen in background.
    """
    if _span_receiver is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    body = await request.body()
    content_type = request.headers.get("content-type", "application/json")

    # ACK immediately — collector moves on, no timeout risk
    background_tasks.add_task(_process_spans_background, body, content_type)

    return Response(content='{"partialSuccess":{}}', media_type="application/json")


async def _process_spans_background(body: bytes, content_type: str) -> None:
    """Parse, persist, and optionally evaluate spans — runs after the HTTP response is sent."""
    receiver = _span_receiver
    if receiver is None:
        return

    spans = receiver.parse_otlp_http_body(body, content_type)
    if not spans:
        log.warning("receive_traces_no_spans_parsed")
        return

    repo = get_repo()
    try:
        repo.save_spans_batch(spans)
    except Exception as exc:
        log.error("receive_traces_save_failed", error=str(exc))
        return  # don't trigger eval if save failed

    trigger = get_trigger()
    pipeline = get_pipeline()
    completed_task_spans = [s for s in spans if trigger.is_completed_task(s)]

    log.info(
        "traces_received",
        total_spans=len(spans),
        eval_queued=len(completed_task_spans),
    )

    now = time.time()
    # Purge stale dedup entries
    stale = [tid for tid, ts in _eval_triggered.items() if now - ts > EVAL_DEDUP_SECONDS]
    for tid in stale:
        del _eval_triggered[tid]

    # Deduplicate: one pipeline run per trace_id per EVAL_DEDUP_SECONDS window
    seen_in_batch: set[str] = set()
    for span in completed_task_spans:
        trace_id = span.get("trace_id", "")
        if not trace_id or trace_id in seen_in_batch:
            continue
        if trace_id in _eval_triggered:
            log.debug("eval_dedup_skip", trace_id=trace_id)
            continue

        seen_in_batch.add(trace_id)
        _eval_triggered[trace_id] = now
        asyncio.create_task(
            _run_eval_in_background(span, pipeline, repo, trigger, spans)
        )

        # Track conversation turns for multi-turn eval
        conv_id = (span.get("attributes") or {}).get("conversation.id", "")
        if conv_id:
            run_id = trigger.get_run_id(span) or ""
            _conversation_tracker[conv_id] = {
                "last_seen": now,
                "run_id":    run_id,
            }


async def _conversation_eval_loop() -> None:
    """Periodic task: fire conversation eval for conversations idle > CONV_IDLE_SECONDS."""
    while True:
        await asyncio.sleep(30)  # check every 30s
        now = time.time()
        ready = [
            (conv_id, meta)
            for conv_id, meta in list(_conversation_tracker.items())
            if now - meta["last_seen"] >= CONV_IDLE_SECONDS
        ]
        for conv_id, meta in ready:
            del _conversation_tracker[conv_id]
            asyncio.create_task(_run_conversation_eval(conv_id, meta["run_id"]))


async def _run_conversation_eval(conversation_id: str, run_id: str) -> None:
    """Run ConversationEvalPipeline for a completed conversation."""
    if _conv_pipeline is None or _repo is None:
        return
    # Resolve run_id if blank
    if not run_id:
        try:
            runs = _repo.get_runs(limit=5)
            run_id = str(runs[0]["run_id"]) if runs else str(__import__("uuid").uuid4())
        except Exception:
            run_id = str(__import__("uuid").uuid4())
    try:
        log.info("conversation_eval_triggered", conversation_id=conversation_id, run_id=run_id)
        _conv_pipeline.run(conversation_id=conversation_id, run_id=run_id)
    except Exception as exc:
        log.error("conversation_eval_failed", conversation_id=conversation_id, error=str(exc))


async def _run_eval_in_background(
    span: dict,
    pipeline: EvalPipeline,
    repo: Repository,
    trigger: EvalTrigger,
    all_spans: list[dict] | None = None,
) -> None:
    """Background task wrapper for eval pipeline execution.

    Waits briefly so ClickHouse's batch window has time to flush before
    the pipeline queries for trace spans.
    """
    import asyncio
    trace_id = span.get("trace_id", "")
    if trace_id in _pipeline_in_progress:
        log.debug("eval_pipeline_already_running", trace_id=trace_id)
        return
    _pipeline_in_progress.add(trace_id)
    await asyncio.sleep(10)  # give ClickHouse batch processor time to flush
    try:
        await trigger.trigger_evaluation(span, pipeline, repo, hint_spans=all_spans)
        # Governance evaluation is handled by the standalone governance-service
        # via its span watcher (polls ClickHouse every 30s for unchecked traces).
    except Exception as exc:
        log.error(
            "background_eval_failed",
            trace_id=trace_id,
            error=str(exc),
        )
    finally:
        _pipeline_in_progress.discard(trace_id)


# ---- Eval Runs -----------------------------------------------------

@app.get("/runs")
async def list_runs(limit: int = 50) -> list[dict]:
    """List recent eval runs."""
    try:
        return get_repo().get_runs(limit=limit)
    except Exception as exc:
        log.error("list_runs_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/runs", status_code=201)
async def create_run(body: CreateRunRequest) -> CreateRunResponse:
    """Create a new eval run and return its run_id."""
    try:
        run_id = get_repo().create_run(
            name=body.name,
            suite=body.suite,
            agent_version=body.agent_version,
            metadata=body.metadata,
        )
        return CreateRunResponse(run_id=run_id, name=body.name)
    except Exception as exc:
        log.error("create_run_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/runs/{run_id}/baseline", status_code=200)
async def set_baseline(run_id: str) -> dict:
    """Promote a run to baseline."""
    try:
        get_repo().set_baseline(run_id)
        return {"run_id": run_id, "is_baseline": True}
    except Exception as exc:
        log.error("set_baseline_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/runs/{run_id}/evaluate", status_code=202)
async def trigger_run_evaluation(
    run_id: str,
    body: EvaluateRunRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Trigger offline evaluation for all traces in a run.

    Evaluation runs in the background; returns 202 Accepted immediately.
    """
    repo = get_repo()
    pipeline = get_pipeline()

    traces = repo.get_traces_for_run(run_id)
    if not traces:
        raise HTTPException(
            status_code=404,
            detail=f"No traces found for run {run_id}",
        )

    for trace_row in traces:
        trace_id = trace_row["trace_id"]
        background_tasks.add_task(
            _evaluate_trace_background,
            trace_id,
            run_id,
            body.mode,
            pipeline,
        )

    log.info("run_evaluation_queued", run_id=run_id, trace_count=len(traces))
    return {
        "run_id": run_id,
        "queued_traces": len(traces),
        "mode": body.mode,
        "status": "queued",
    }


async def _evaluate_trace_background(
    trace_id: str,
    run_id: str,
    mode: str,
    pipeline: EvalPipeline,
) -> None:
    if trace_id in _pipeline_in_progress:
        log.debug("offline_eval_skipped_in_progress", trace_id=trace_id)
        return
    _pipeline_in_progress.add(trace_id)
    try:
        pipeline.run(trace_id=trace_id, run_id=run_id, mode=mode)
    except Exception as exc:
        log.error(
            "offline_eval_failed", trace_id=trace_id, run_id=run_id, error=str(exc)
        )
    finally:
        _pipeline_in_progress.discard(trace_id)


@app.get("/runs/{run_id}/scores")
async def get_run_scores(run_id: str) -> list[dict]:
    """Return per-metric aggregated scores for a run."""
    try:
        return get_repo().get_run_scores(run_id)
    except Exception as exc:
        log.error("get_run_scores_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/runs/{run_id}/regression")
async def get_run_regression(run_id: str) -> dict:
    """
    Compare a run's scores against the current baseline.

    Returns per-metric deltas and a regression summary.
    """
    repo = get_repo()
    comparator = get_comparator()

    baseline = repo.get_baseline_run()
    if baseline is None:
        raise HTTPException(
            status_code=404,
            detail="No baseline run set. Use PUT /runs/{run_id}/baseline first.",
        )

    baseline_run_id = str(baseline["run_id"])
    if baseline_run_id == run_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot compare a run against itself as baseline",
        )

    comparisons = comparator.compare(run_id, baseline_run_id, repo)
    summary = comparator.get_regression_summary(comparisons)

    return {
        "run_id": run_id,
        "baseline_run_id": baseline_run_id,
        "comparisons": comparisons,
        "summary": summary,
    }


# ---- Trace scores --------------------------------------------------

@app.get("/traces/{trace_id}/scores")
async def get_trace_scores(trace_id: str) -> list[dict]:
    """Return all eval scores for a specific trace."""
    try:
        return get_repo().get_scores_for_trace(trace_id)
    except Exception as exc:
        log.error("get_trace_scores_failed", trace_id=trace_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
