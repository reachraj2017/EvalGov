"""All database read/write operations for the eval platform."""

import uuid
from datetime import datetime, timezone
from typing import Optional

import structlog

from .clickhouse_client import ClickHouseClient, get_client

log = structlog.get_logger(__name__)


class Repository:
    """Encapsulates all ClickHouse read/write operations."""

    def __init__(self, client: Optional[ClickHouseClient] = None) -> None:
        self._ch = client or get_client()

    # ------------------------------------------------------------------
    # Spans / Traces
    # ------------------------------------------------------------------

    def save_span(self, span: dict) -> None:
        """Insert a single span into otel.otel_traces."""
        self.save_spans_batch([span])

    def save_spans_batch(self, spans: list[dict]) -> None:
        """Bulk-insert spans into otel.otel_traces."""
        if not spans:
            return
        query = """
            INSERT INTO otel.otel_traces
                (TraceId, SpanId, ParentSpanId, SpanName, ServiceName,
                 Duration, StatusCode, SpanAttributes, Timestamp)
            VALUES
        """
        data = []
        for s in spans:
            data.append((
                s.get("trace_id", ""),
                s.get("span_id", ""),
                s.get("parent_span_id", ""),
                s.get("span_name", ""),
                s.get("service_name", ""),
                int(s.get("duration_ns", 0)),
                s.get("status_code", "STATUS_CODE_UNSET"),
                s.get("attributes", {}),
                s.get("timestamp", datetime.now(timezone.utc)),
            ))
        try:
            self._ch.execute_many(query, data)
            log.info("spans_saved", count=len(data))
        except Exception as exc:
            log.error("save_spans_batch_failed", error=str(exc), count=len(spans))
            raise

    def get_trace_spans(self, trace_id: str) -> list[dict]:
        """Return all spans for a trace, one row per SpanId.

        otel_traces is ReplacingMergeTree. The eval_runner writes spans via
        save_spans_batch in addition to the OTel Collector's clickhouse exporter,
        so each span can appear twice until the background merge runs.
        LIMIT 1 BY SpanId deduplicates at read time regardless of merge state.
        """
        query = """
            SELECT
                TraceId        AS trace_id,
                SpanId         AS span_id,
                ParentSpanId   AS parent_span_id,
                SpanName       AS span_name,
                ServiceName    AS service_name,
                Duration       AS duration_ns,
                StatusCode     AS status_code,
                SpanAttributes AS attributes,
                Timestamp      AS timestamp
            FROM otel.otel_traces
            WHERE TraceId = %(trace_id)s
            ORDER BY Timestamp ASC
            LIMIT 1 BY SpanId
        """
        try:
            return self._ch.fetch_all(query, {"trace_id": trace_id})
        except Exception as exc:
            log.error("get_trace_spans_failed", trace_id=trace_id, error=str(exc))
            return []

    def get_traces_for_run(self, run_id: str) -> list[dict]:
        """Return distinct trace_ids associated with a run via eval_scores."""
        query = """
            SELECT DISTINCT trace_id
            FROM otel.eval_scores
            WHERE run_id = %(run_id)s
        """
        try:
            return self._ch.fetch_all(query, {"run_id": run_id})
        except Exception as exc:
            log.error("get_traces_for_run_failed", run_id=run_id, error=str(exc))
            return []

    # ------------------------------------------------------------------
    # Eval Runs
    # ------------------------------------------------------------------

    def create_run(
        self,
        name: str,
        suite: str = "",
        agent_version: str = "",
        metadata: Optional[dict] = None,
    ) -> str:
        """Insert a new eval run and return its run_id (UUID string)."""
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        query = """
            INSERT INTO otel.eval_runs
                (run_id, name, suite, agent_version, created_at, is_baseline, metadata)
            VALUES
        """
        data = [(
            run_id,
            name,
            suite,
            agent_version,
            now,
            0,
            metadata or {},
        )]
        try:
            self._ch.execute_many(query, data)
            log.info("run_created", run_id=run_id, name=name)
            return run_id
        except Exception as exc:
            log.error("create_run_failed", error=str(exc))
            raise

    def get_runs(self, limit: int = 50) -> list[dict]:
        """List recent eval runs."""
        query = """
            SELECT
                toString(run_id)   AS run_id,
                name,
                suite,
                agent_version,
                created_at,
                is_baseline,
                metadata
            FROM otel.eval_runs
            ORDER BY created_at DESC
            LIMIT %(limit)s
        """
        try:
            return self._ch.fetch_all(query, {"limit": limit})
        except Exception as exc:
            log.error("get_runs_failed", error=str(exc))
            return []

    def get_baseline_run(self) -> Optional[dict]:
        """Return the run currently marked as baseline, or None."""
        query = """
            SELECT
                toString(run_id)   AS run_id,
                name,
                suite,
                agent_version,
                created_at,
                is_baseline,
                metadata
            FROM otel.eval_runs
            WHERE is_baseline = 1
            ORDER BY created_at DESC
            LIMIT 1
        """
        try:
            return self._ch.fetch_one(query)
        except Exception as exc:
            log.error("get_baseline_run_failed", error=str(exc))
            return None

    def set_baseline(self, run_id: str) -> None:
        """Mark run_id as baseline and clear all others."""
        try:
            # ClickHouse does not support standard UPDATE with WHERE NOT on
            # MergeTree easily; use ALTER TABLE UPDATE (mutations).
            self._ch.execute(
                "ALTER TABLE otel.eval_runs UPDATE is_baseline = 0 WHERE run_id != %(run_id)s",
                {"run_id": run_id},
            )
            self._ch.execute(
                "ALTER TABLE otel.eval_runs UPDATE is_baseline = 1 WHERE run_id = %(run_id)s",
                {"run_id": run_id},
            )
            log.info("baseline_set", run_id=run_id)
        except Exception as exc:
            log.error("set_baseline_failed", run_id=run_id, error=str(exc))
            raise

    # ------------------------------------------------------------------
    # Eval Scores
    # ------------------------------------------------------------------

    def save_score(
        self,
        trace_id: str,
        run_id: str,
        span_id: str,
        evaluator: str,
        metric: str,
        score: float,
        reasoning: str,
        eval_type: str,
    ) -> None:
        """Insert an eval score row."""
        score_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        query = """
            INSERT INTO otel.eval_scores
                (id, trace_id, run_id, span_id, evaluator, metric,
                 score, reasoning, eval_type, evaluated_at)
            VALUES
        """
        data = [(
            score_id,
            trace_id,
            run_id,
            span_id,
            evaluator,
            metric,
            float(score),
            reasoning,
            eval_type,
            now,
        )]
        try:
            self._ch.execute_many(query, data)
            log.debug(
                "score_saved",
                trace_id=trace_id,
                evaluator=evaluator,
                metric=metric,
                score=score,
            )
        except Exception as exc:
            log.error("save_score_failed", error=str(exc))
            raise

    def get_scores_for_trace(self, trace_id: str) -> list[dict]:
        """Return all eval score rows for a trace."""
        query = """
            SELECT
                toString(id)   AS id,
                trace_id,
                toString(run_id) AS run_id,
                span_id,
                evaluator,
                metric,
                score,
                reasoning,
                eval_type,
                evaluated_at
            FROM otel.eval_scores
            WHERE trace_id = %(trace_id)s
            ORDER BY evaluated_at DESC
        """
        try:
            return self._ch.fetch_all(query, {"trace_id": trace_id})
        except Exception as exc:
            log.error("get_scores_for_trace_failed", trace_id=trace_id, error=str(exc))
            return []

    def get_run_scores(self, run_id: str) -> list[dict]:
        """Return per-metric aggregated scores for a run."""
        query = """
            SELECT
                metric,
                evaluator,
                eval_type,
                avg(score)   AS avg_score,
                min(score)   AS min_score,
                max(score)   AS max_score,
                count()      AS sample_count
            FROM otel.eval_scores
            WHERE run_id = %(run_id)s
            GROUP BY metric, evaluator, eval_type
            ORDER BY metric
        """
        try:
            return self._ch.fetch_all(query, {"run_id": run_id})
        except Exception as exc:
            log.error("get_run_scores_failed", run_id=run_id, error=str(exc))
            return []

    def get_regression(self, run_id: str, baseline_run_id: str) -> list[dict]:
        """Compute per-metric score deltas between run and baseline."""
        query = """
            SELECT
                cur.metric                        AS metric,
                cur.avg_score                     AS current_score,
                base.avg_score                    AS baseline_score,
                (cur.avg_score - base.avg_score)  AS delta
            FROM (
                SELECT metric, avg(score) AS avg_score
                FROM otel.eval_scores
                WHERE run_id = %(run_id)s
                GROUP BY metric
            ) AS cur
            INNER JOIN (
                SELECT metric, avg(score) AS avg_score
                FROM otel.eval_scores
                WHERE run_id = %(baseline_run_id)s
                GROUP BY metric
            ) AS base ON cur.metric = base.metric
            ORDER BY delta ASC
        """
        try:
            return self._ch.fetch_all(
                query, {"run_id": run_id, "baseline_run_id": baseline_run_id}
            )
        except Exception as exc:
            log.error(
                "get_regression_failed",
                run_id=run_id,
                baseline_run_id=baseline_run_id,
                error=str(exc),
            )
            return []

    # ------------------------------------------------------------------
    # Prompt Evaluations
    # ------------------------------------------------------------------

    def save_prompt_eval(
        self,
        trace_id: str,
        span_id: str,
        run_id: str,
        agent_name: str,
        model: str,
        prompt_text: str,
        response_text: str,
        latency_ms: int,
        prompt_tokens: int,
        completion_tokens: int,
        scores: dict,
    ) -> None:
        """Insert a prompt evaluation row with all metric scores.

        Skips the insert if (trace_id, span_id) already exists — prevents
        duplicate rows from OTel Collector retries or overlapping span batches.
        """
        import hashlib

        # Dedup guard: skip only if a row already exists WITH tokens.
        # A previous insert may have landed with 0 tokens (LLM spans not yet
        # flushed to ClickHouse). Allow a re-insert when new data has tokens.
        try:
            existing = self._ch.execute(
                """SELECT count() AS cnt,
                          max(prompt_tokens + completion_tokens) AS max_tok
                   FROM otel.prompt_evals
                   WHERE trace_id = %(tid)s AND span_id = %(sid)s""",
                {"tid": trace_id, "sid": span_id},
            )
            if existing:
                cnt     = int(existing[0].get("cnt", 0))
                max_tok = int(existing[0].get("max_tok", 0))
                new_tok = prompt_tokens + completion_tokens
                # Skip if: already have a row with tokens, OR new data also has no tokens
                if cnt > 0 and (max_tok > 0 or new_tok == 0):
                    log.info("prompt_eval_skipped_duplicate",
                             trace_id=trace_id, span_id=span_id, max_tok=max_tok)
                    return
        except Exception as exc:
            log.warning("prompt_eval_dedup_check_failed", error=str(exc))
            # Proceed with insert if check fails — better a dup than a missing row

        prompt_hash = hashlib.sha256(prompt_text.encode()).hexdigest()[:16]
        now = datetime.now(timezone.utc)

        query = """
            INSERT INTO otel.prompt_evals
                (prompt_eval_id, prompt_hash, trace_id, span_id, run_id,
                 agent_name, model, prompt_text, response_text,
                 latency_ms, prompt_tokens, completion_tokens, scores, created_at)
            VALUES
        """
        data = [(
            str(uuid.uuid4()),
            prompt_hash,
            trace_id,
            span_id,
            run_id,
            agent_name,
            model,
            prompt_text,
            response_text,
            int(latency_ms),
            int(prompt_tokens),
            int(completion_tokens),
            {k: float(v) for k, v in scores.items()},
            now,
        )]
        try:
            self._ch.execute_many(query, data)
            log.info("prompt_eval_saved", trace_id=trace_id, agent=agent_name, scores=len(scores))
        except Exception as exc:
            log.error("save_prompt_eval_failed", error=str(exc))

    def get_prompt_evals(
        self,
        agent_name: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """Return recent prompt eval rows, optionally filtered by agent."""
        params: dict = {"limit": limit, "offset": offset}
        where = "WHERE agent_name = %(agent_name)s" if agent_name else ""
        if agent_name:
            params["agent_name"] = agent_name
        query = f"""
            SELECT
                prompt_eval_id,
                prompt_hash,
                trace_id,
                run_id,
                agent_name,
                model,
                prompt_text,
                response_text,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                scores,
                created_at
            FROM otel.prompt_evals
            {where}
            ORDER BY created_at DESC
            LIMIT %(limit)s OFFSET %(offset)s
        """
        try:
            return self._ch.fetch_all(query, params)
        except Exception as exc:
            log.error("get_prompt_evals_failed", error=str(exc))
            return []

    def get_prompt_variants(self, prompt_hash: str) -> list[dict]:
        """Return all evals for the same prompt hash (repeated prompt runs)."""
        query = """
            SELECT
                prompt_eval_id,
                trace_id,
                run_id,
                agent_name,
                model,
                response_text,
                latency_ms,
                prompt_tokens,
                completion_tokens,
                scores,
                created_at
            FROM otel.prompt_evals
            WHERE prompt_hash = %(prompt_hash)s
            ORDER BY created_at DESC
        """
        try:
            return self._ch.fetch_all(query, {"prompt_hash": prompt_hash})
        except Exception as exc:
            log.error("get_prompt_variants_failed", error=str(exc))
            return []

    # ------------------------------------------------------------------
    # Benchmarks
    # ------------------------------------------------------------------

    def get_benchmarks(self, suite: Optional[str] = None) -> list[dict]:
        """Return benchmark definitions, optionally filtered by suite."""
        if suite:
            query = """
                SELECT
                    toString(benchmark_id) AS benchmark_id,
                    suite, name, task_input, expected_output,
                    rubric, difficulty, tags
                FROM otel.benchmarks
                WHERE suite = %(suite)s
                ORDER BY name
            """
            params: dict = {"suite": suite}
        else:
            query = """
                SELECT
                    toString(benchmark_id) AS benchmark_id,
                    suite, name, task_input, expected_output,
                    rubric, difficulty, tags
                FROM otel.benchmarks
                ORDER BY suite, name
            """
            params = {}
        try:
            return self._ch.fetch_all(query, params)
        except Exception as exc:
            log.error("get_benchmarks_failed", suite=suite, error=str(exc))
            return []

    def get_benchmark_for_input(self, task_input: str) -> Optional[dict]:
        """Look up a benchmark by exact task_input match — used to get expected_output and rubric."""
        query = """
            SELECT expected_output, rubric
            FROM otel.benchmarks
            WHERE task_input = %(task_input)s AND active = 1
            LIMIT 1
        """
        try:
            return self._ch.fetch_one(query, {"task_input": task_input})
        except Exception as exc:
            log.warning("get_benchmark_for_input_failed", error=str(exc))
            return None

    # ------------------------------------------------------------------
    # Conversation turns (multi-turn eval)
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Governance
    # ------------------------------------------------------------------

    def ensure_gov_tables(self) -> None:
        """Create the four governance tables if they don't exist yet.

        Called from GovernanceRunner.__init__ so existing deployments get
        the tables without a full ClickHouse restart.
        """
        ddl_list = [
            """
            CREATE TABLE IF NOT EXISTS otel.gov_metric_snapshots (
                trace_id    String,
                span_id     String        DEFAULT '',
                run_id      String        DEFAULT '',
                metric      String,
                value       Float32,
                detail      String        DEFAULT '',
                ts          DateTime      DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY (trace_id, metric, ts)
            SETTINGS index_granularity = 8192
            """,
            """
            CREATE TABLE IF NOT EXISTS otel.gov_policy_decisions (
                decision_id String        DEFAULT generateUUIDv4(),
                trace_id    String        DEFAULT '',
                run_id      String        DEFAULT '',
                metric      LowCardinality(String),
                decision    LowCardinality(String),
                value       Float32,
                threshold   Float32,
                message     String        DEFAULT '',
                ts          DateTime      DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY (run_id, trace_id, metric, ts)
            SETTINGS index_granularity = 8192
            """,
            """
            CREATE TABLE IF NOT EXISTS otel.gov_audit_log (
                event_id    String        DEFAULT generateUUIDv4(),
                trace_id    String        DEFAULT '',
                span_id     String        DEFAULT '',
                run_id      String        DEFAULT '',
                event_type  String,
                detail      String        DEFAULT '',
                ts          DateTime      DEFAULT now()
            ) ENGINE = MergeTree()
            ORDER BY (trace_id, event_type, ts)
            SETTINGS index_granularity = 8192
            """,
            """
            CREATE TABLE IF NOT EXISTS otel.gov_hitl_queue (
                request_id  String        DEFAULT generateUUIDv4(),
                trace_id    String,
                span_id     String        DEFAULT '',
                run_id      String        DEFAULT '',
                risk_tier   LowCardinality(String) DEFAULT 'low',
                action_type String        DEFAULT '',
                payload     String        DEFAULT '',
                status      LowCardinality(String) DEFAULT 'pending',
                reviewer    String        DEFAULT '',
                notes       String        DEFAULT '',
                created_at  DateTime      DEFAULT now(),
                decided_at  DateTime      DEFAULT '1970-01-01 00:00:00'
            ) ENGINE = MergeTree()
            ORDER BY (status, risk_tier, created_at)
            SETTINGS index_granularity = 8192
            """,
        ]
        for ddl in ddl_list:
            try:
                self._ch.execute(ddl)
            except Exception as exc:
                log.warning("gov_table_ddl_failed", error=str(exc))

    def save_gov_metric(
        self,
        trace_id: str,
        span_id: str,
        run_id: str,
        metric: str,
        value: float,
        detail: str,
    ) -> None:
        """Insert a governance metric snapshot row."""
        query = """
            INSERT INTO otel.gov_metric_snapshots
                (trace_id, span_id, run_id, metric, value, detail, ts)
            VALUES
        """
        data = [(trace_id, span_id, run_id, metric, float(value), detail,
                 datetime.now(timezone.utc))]
        try:
            self._ch.execute_many(query, data)
        except Exception as exc:
            log.error("save_gov_metric_failed", trace_id=trace_id, metric=metric, error=str(exc))

    def save_gov_policy_decision(
        self,
        trace_id: str,
        run_id: str,
        metric: str,
        decision: str,
        value: float,
        threshold: float,
        message: str,
    ) -> None:
        """Insert a governance policy decision row."""
        now = datetime.now(timezone.utc)
        query = """
            INSERT INTO otel.gov_policy_decisions
                (decision_id, trace_id, run_id, metric, decision,
                 value, threshold, message, ts)
            VALUES
        """
        data = [(str(uuid.uuid4()), trace_id, run_id, metric, decision,
                 float(value), float(threshold), message, now)]
        try:
            self._ch.execute_many(query, data)
        except Exception as exc:
            log.error("save_gov_policy_decision_failed",
                      trace_id=trace_id, metric=metric, error=str(exc))

    def save_gov_audit_log(
        self,
        trace_id: str,
        span_id: str,
        run_id: str,
        event_type: str,
        detail: str,
    ) -> None:
        """Append a governance audit log entry."""
        now = datetime.now(timezone.utc)
        query = """
            INSERT INTO otel.gov_audit_log
                (event_id, trace_id, span_id, run_id, event_type, detail, ts)
            VALUES
        """
        data = [(str(uuid.uuid4()), trace_id, span_id, run_id, event_type, detail, now)]
        try:
            self._ch.execute_many(query, data)
        except Exception as exc:
            log.error("save_gov_audit_log_failed",
                      trace_id=trace_id, event_type=event_type, error=str(exc))

    def save_gov_hitl_request(
        self,
        trace_id: str,
        span_id: str,
        run_id: str,
        risk_tier: str,
        action_type: str,
        payload: str,
    ) -> str:
        """Queue a HITL review request. Returns the new request_id."""
        request_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        query = """
            INSERT INTO otel.gov_hitl_queue
                (request_id, trace_id, span_id, run_id, risk_tier,
                 action_type, payload, status, created_at)
            VALUES
        """
        data = [(request_id, trace_id, span_id, run_id, risk_tier,
                 action_type, payload, "pending", now)]
        try:
            self._ch.execute_many(query, data)
            return request_id
        except Exception as exc:
            log.error("save_gov_hitl_request_failed", trace_id=trace_id, error=str(exc))
            return ""

    def update_gov_hitl_decision(
        self,
        request_id: str,
        status: str,
        reviewer: str,
        notes: str,
    ) -> None:
        """Approve or reject a HITL queue entry."""
        try:
            self._ch.execute(
                "ALTER TABLE otel.gov_hitl_queue "
                "UPDATE status = %(status)s, reviewer = %(reviewer)s, "
                "notes = %(notes)s, decided_at = now() "
                "WHERE request_id = %(request_id)s",
                {"status": status, "reviewer": reviewer,
                 "notes": notes, "request_id": request_id},
            )
        except Exception as exc:
            log.error("update_gov_hitl_decision_failed",
                      request_id=request_id, error=str(exc))

    def get_gov_policy_decisions(
        self,
        run_id: str = "",
        trace_id: str = "",
        limit: int = 500,
    ) -> list[dict]:
        """Return policy decisions filtered by run or trace."""
        conditions: list[str] = []
        params: dict = {"limit": limit}
        if run_id:
            conditions.append("run_id = %(run_id)s")
            params["run_id"] = run_id
        if trace_id:
            conditions.append("trace_id = %(trace_id)s")
            params["trace_id"] = trace_id
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        query = f"""
            SELECT decision_id, trace_id, run_id, metric,
                   decision, value, threshold, message, ts
            FROM otel.gov_policy_decisions
            {where}
            ORDER BY ts DESC
            LIMIT %(limit)s
        """
        try:
            return self._ch.fetch_all(query, params)
        except Exception as exc:
            log.error("get_gov_policy_decisions_failed", error=str(exc))
            return []

    def get_gov_metrics(self, trace_id: str) -> list[dict]:
        """Return all governance metric snapshots for a trace."""
        query = """
            SELECT trace_id, span_id, run_id, metric, value, detail, ts
            FROM otel.gov_metric_snapshots
            WHERE trace_id = %(trace_id)s
            ORDER BY metric, ts
        """
        try:
            return self._ch.fetch_all(query, {"trace_id": trace_id})
        except Exception as exc:
            log.error("get_gov_metrics_failed", trace_id=trace_id, error=str(exc))
            return []

    def get_gov_audit_log(self, limit: int = 100) -> list[dict]:
        """Return recent governance audit log entries."""
        query = """
            SELECT event_id, trace_id, span_id, run_id, event_type, detail, ts
            FROM otel.gov_audit_log
            ORDER BY ts DESC
            LIMIT %(limit)s
        """
        try:
            return self._ch.fetch_all(query, {"limit": limit})
        except Exception as exc:
            log.error("get_gov_audit_log_failed", error=str(exc))
            return []

    def get_gov_hitl_queue(
        self,
        status: str = "pending",
        limit: int = 100,
    ) -> list[dict]:
        """Return HITL queue entries filtered by status."""
        query = """
            SELECT request_id, trace_id, span_id, run_id, risk_tier,
                   action_type, payload, status, reviewer, notes,
                   created_at, decided_at
            FROM otel.gov_hitl_queue
            WHERE status = %(status)s
            ORDER BY risk_tier DESC, created_at ASC
            LIMIT %(limit)s
        """
        try:
            return self._ch.fetch_all(query, {"status": status, "limit": limit})
        except Exception as exc:
            log.error("get_gov_hitl_queue_failed", status=status, error=str(exc))
            return []

    def get_conversation_turns(self, conversation_id: str) -> list[dict]:
        """Return ordered agent.task spans for a conversation, used by multi-turn evaluators."""
        query = """
            SELECT
                TraceId                              AS trace_id,
                SpanAttributes['task.input']         AS user_input,
                SpanAttributes['task.output']        AS agent_output,
                SpanAttributes['agent.role']         AS agent_role,
                Timestamp
            FROM otel.otel_traces
            WHERE SpanAttributes['conversation.id'] = %(conv_id)s
              AND SpanName = 'agent.task'
            ORDER BY Timestamp ASC
        """
        try:
            return self._ch.fetch_all(query, {"conv_id": conversation_id})
        except Exception as exc:
            log.error("get_conversation_turns_failed", conversation_id=conversation_id, error=str(exc))
            return []
